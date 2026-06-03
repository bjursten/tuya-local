"""
API for Tuya Local devices.
"""

import asyncio
import logging
from asyncio.exceptions import CancelledError
from base64 import b64decode, b64encode
from threading import Lock
from time import time

import tinytuya
from homeassistant.const import (
    CONF_HOST,
    CONF_NAME,
    EVENT_HOMEASSISTANT_STARTED,
    EVENT_HOMEASSISTANT_STOP,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store

from .const import (
    API_PROTOCOL_VERSIONS,
    CONF_DEVICE_CID,
    CONF_DEVICE_ID,
    CONF_LOCAL_KEY,
    CONF_MANUFACTURER,
    CONF_MODEL,
    CONF_POLL_ONLY,
    CONF_PROTOCOL_VERSION,
    DOMAIN,
)
from .helpers.config import get_device_id
from .helpers.device_config import _apply_crc16_modbus, possible_matches
from .helpers.log import log_json

_LOGGER = logging.getLogger(__name__)

ISC028_DP102_CONFIG = "inkbird_isc028bw_smokercontrol"
DP102_STORE_VERSION = 1
DP102_DEFAULT_TARGET_C = 107.2
DP102_CRC_OFFSET = 90
# Idle template (°C mode); bytes 0/1/7-8/80/CRC adjusted at runtime.
_DP102_TEMPLATE_B64 = (
    "AQAAAAAAAO4HDBcMFwwXDBcMFwAAAAAAAAAAAAAAAAAAAAUFBQUFAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAEAAAAASSYCAwQZlf8="
)


def _decode_dp102_blob(blob_b64):
    """Return DP 102 byte fields from a base64 blob."""
    if not blob_b64:
        return None
    try:
        data = b64decode(blob_b64)
    except Exception:
        return None
    if len(data) < 81:
        return None
    return {
        "fan": data[0],
        "unit_c": data[1] == 0,
        "hold_f10": int.from_bytes(data[7:9], "little"),
        "sound": data[80],
    }


def build_dp102_blob(
    hold_f10,
    fan_byte=None,
    unit_c=True,
    sound_on=False,
    base_b64=None,
):
    """Build a full DP 102 blob; hold_f10 is always °F×10 (internal format)."""
    if base_b64:
        data = bytearray(b64decode(base_b64))
    else:
        data = bytearray(b64decode(_DP102_TEMPLATE_B64))
    if fan_byte is not None:
        data[0] = fan_byte
    data[1] = 0 if unit_c else 1
    data[7:9] = int(hold_f10).to_bytes(2, "little")
    data[80] = 1 if sound_on else 0
    data = bytearray(_apply_crc16_modbus(bytes(data), DP102_CRC_OFFSET))
    return b64encode(bytes(data)).decode()


def build_default_dp102_blob():
    """Default ISC-028-BW settings: fan on, °C, 107.2 °C hold, sound off."""
    hold_f10 = round((DP102_DEFAULT_TARGET_C * 9 / 5 + 32) * 10)
    return build_dp102_blob(hold_f10, fan_byte=1, unit_c=True, sound_on=False)


def synthesize_dp102_cache_blob(persisted_b64=None, dp101_b64=None):
    """Build cache-only blob: real hold if known, °C + sound off, fan from DP 101."""
    hold_f10 = round((DP102_DEFAULT_TARGET_C * 9 / 5 + 32) * 10)
    fan_byte = 1
    if persisted_b64:
        fields = _decode_dp102_blob(persisted_b64)
        if fields:
            hold_f10 = fields["hold_f10"]
            fan_byte = fields["fan"]
    if dp101_b64:
        try:
            raw101 = b64decode(dp101_b64)
            if len(raw101) >= 11:
                fan_byte = 1 if raw101[10] > 0 else 0
        except Exception:
            pass
    return build_dp102_blob(
        hold_f10,
        fan_byte=fan_byte,
        unit_c=True,
        sound_on=False,
        base_b64=persisted_b64,
    )


def _collect_possible_matches(cached_state, product_ids):
    """Collect possible matches from generator into an array."""
    return list(possible_matches(cached_state, product_ids))


class TuyaLocalDevice(object):
    def __init__(
        self,
        name,
        dev_id,
        address,
        local_key,
        protocol_version,
        dev_cid,
        hass: HomeAssistant,
        poll_only=False,
        manufacturer=None,
        model=None,
    ):
        """
        Represents a Tuya-based device.

        Args:
            name (str): The device name.
            dev_id (str): The device id.
            address (str): The network address.
            local_key (str): The encryption key.
            protocol_version (str | number): The protocol version.
            dev_cid (str): The sub device id.
            hass (HomeAssistant): The Home Assistant instance.
            poll_only (bool): True if the device should be polled only.
            manufacturer (str | None): The device manufacturer, if known.
            model (str | None): The device model, if known.
        """
        self._name = name
        self._manufacturer = manufacturer
        self._model = model
        self._children = []
        self._force_dps = []
        self._config_type = None
        self._dp102_store = None
        self._persisted_dp102 = None
        self._dp102_verified_from_device = False
        self._dp102_on_101_scheduled = False
        self._dp102_write_scheduled = False
        self._fetch_missing_scheduled = False
        self._dp102_pending_device_write = False
        self._dp102_session_written = False
        self._product_ids = []
        self._running = False
        self._shutdown_listener = None
        self._startup_listener = None
        self._api_protocol_version_index = None
        self._api_protocol_working = False
        self._api_working_protocol_failures = 0
        self.dev_cid = dev_cid
        try:
            if dev_cid:
                if hass.data[DOMAIN].get(dev_id) and name != "Test":
                    parent = hass.data[DOMAIN][dev_id]["tuyadevice"]
                    parent_lock = hass.data[DOMAIN][dev_id].get(
                        "tuyadevicelock", asyncio.Lock()
                    )
                else:
                    parent = tinytuya.Device(dev_id, address, local_key)
                    parent_lock = asyncio.Lock()
                    if name != "Test":
                        hass.data[DOMAIN][dev_id] = {
                            "tuyadevice": parent,
                            "tuyadevicelock": parent_lock,
                        }
                self._api = tinytuya.Device(
                    dev_cid,
                    cid=dev_cid,
                    parent=parent,
                )
                self._api_lock = parent_lock
            else:
                if hass.data[DOMAIN].get(dev_id) and name != "Test":
                    self._api = hass.data[DOMAIN][dev_id]["tuyadevice"]
                    self._api_lock = hass.data[DOMAIN][dev_id].get(
                        "tuyadevicelock", asyncio.Lock()
                    )
                else:
                    self._api = tinytuya.Device(dev_id, address, local_key)
                    self._api_lock = asyncio.Lock()
                    if name != "Test":
                        hass.data[DOMAIN][dev_id] = {
                            "tuyadevice": self._api,
                            "tuyadevicelock": self._api_lock,
                        }
        except Exception as e:
            _LOGGER.error(
                "%s: %s while initialising device %s",
                type(e).__name__,
                e,
                dev_id,
            )
            raise e

        # we handle retries at a higher level so we can rotate protocol version
        # on the other hand, protocol 3.4 devices send encrypted null ACKs that
        # often get mixed in, so we need to retry a couple of times before resorting
        # to recovery measures that seem to make things worse.
        self._api.set_socketRetryLimit(2)
        if self._api.parent:
            # Retries cause problems for other children of the parent device
            self._api.parent.set_socketRetryLimit(1)

        self._refresh_task = None
        self._protocol_configured = protocol_version
        self._poll_only = poll_only
        self._temporary_poll = False
        self._reset_cached_state()

        self._hass = hass

        # API calls to update Tuya devices are asynchronous and non-blocking.
        # This means you can send a change and immediately request an updated
        # state (like HA does), but because it has not yet finished processing
        # you will be returned the old state.
        # The solution is to keep a temporary list of changed properties that
        # we can overlay onto the state while we wait for the board to update
        # its switches.
        self._FAKE_IT_TIMEOUT = 5
        self._CACHE_TIMEOUT = 30
        self._HEARTBEAT_INTERVAL = 10
        # More attempts are needed in auto mode so we can cycle through all
        # the possibilities a couple of times
        self._AUTO_CONNECTION_ATTEMPTS = len(API_PROTOCOL_VERSIONS) * 2 + 1
        self._SINGLE_PROTO_CONNECTION_ATTEMPTS = 3
        # The number of failures from a working protocol before retrying other protocols.
        self._AUTO_FAILURE_RESET_COUNT = 10
        self._lock = Lock()

    @property
    def name(self):
        return self._name

    @property
    def unique_id(self):
        """Return the unique id for this device (the dev_id or dev_cid)."""
        return self.dev_cid or self._api.id

    @property
    def device_info(self):
        """Return the device information for this device."""
        info = {
            "identifiers": {(DOMAIN, self.unique_id)},
            "name": self.name,
            "manufacturer": self._manufacturer or "Tuya",
        }
        if self._model:
            info["model"] = self._model
        return info

    @property
    def dp102_persist_enabled(self):
        return self._config_type == ISC028_DP102_CONFIG

    def set_config_type(self, config_type):
        self._config_type = config_type

    @property
    def has_returned_state(self):
        """Return True if the device has returned some state."""
        cached = self._get_cached_state()
        return len(cached) > 1 or cached.get("updated_at", 0) > 0

    @callback
    def actually_start(self, event=None):
        _LOGGER.debug("Starting monitor loop for %s", self.name)
        self._running = True
        self._shutdown_listener = self._hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STOP, self.async_stop
        )
        if not self._refresh_task:
            self._refresh_task = self._hass.async_create_task(self.receive_loop())

    def start(self):
        if self._hass.is_stopping:
            return
        elif self._hass.is_running:
            if self._startup_listener:
                self._startup_listener()
                self._startup_listener = None
            self.actually_start()
        else:
            self._startup_listener = self._hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STARTED, self.actually_start
            )

    async def async_stop(self, event=None):
        _LOGGER.debug("Stopping monitor loop for %s", self.name)
        self._running = False
        self._children.clear()
        self._force_dps.clear()
        if self._refresh_task:
            self._api.set_socketPersistent(False)
            if self._api.parent:
                self._api.parent.set_socketPersistent(False)
            await self._refresh_task
        _LOGGER.debug("Monitor loop for %s stopped", self.name)
        self._refresh_task = None

    def register_entity(self, entity):
        # If this is the first child entity to register, and HA is still
        # starting, refresh the device state so it shows as available without
        # waiting for startup to complete.
        should_poll = len(self._children) == 0 and not self._hass.is_running

        self._children.append(entity)
        for dp in entity._config.dps():
            if dp.force and int(dp.id) not in self._force_dps:
                self._force_dps.append(int(dp.id))

        if not self._running and not self._startup_listener:
            self.start()
        if self.has_returned_state:
            entity.async_schedule_update_ha_state()
        elif should_poll:
            entity.async_schedule_update_ha_state(True)

    async def async_unregister_entity(self, entity):
        self._children.remove(entity)
        if not self._children:
            try:
                await self.async_stop()
            except CancelledError:
                pass

    async def receive_loop(self):
        """Coroutine wrapper for async_receive generator."""
        try:
            async for poll in self.async_receive():
                if isinstance(poll, dict):
                    _LOGGER.debug(
                        "%s received %s",
                        self.name,
                        log_json(poll),
                    )
                    full_poll = poll.pop("full_poll", False)
                    poll_dps = self._poll_dps(poll)
                    self._cached_state = self._cached_state | poll_dps
                    self._cached_state["updated_at"] = time()
                    self._remove_properties_from_pending_updates(poll_dps)
                    self._handle_dp102_poll(poll_dps)

                    for entity in self._children:
                        # let entities trigger off poll contents directly
                        try:
                            entity.on_receive(poll_dps, full_poll)
                        except Exception as e:
                            # Don't let exceptions thrown by the entities interrupt the communication loop
                            # Just log them and move on.
                            _LOGGER.exception(
                                "%s on_receive error for entity %s: %s",
                                self.name,
                                entity.entity_id,
                                e,
                            )
                        if full_poll:
                            self._clear_stale_dps_on_full_poll(poll_dps)
                        entity.schedule_update_ha_state()
                else:
                    _LOGGER.debug(
                        "%s received non data %s",
                        self.name,
                        log_json(poll),
                    )
            _LOGGER.warning("%s receive loop has terminated", self.name)

        except Exception as t:
            _LOGGER.exception(
                "%s receive loop terminated by exception %s", self.name, t
            )
            self._api.set_socketPersistent(False)
            if self._api.parent:
                self._api.parent.set_socketPersistent(False)

    @property
    def should_poll(self):
        return self._poll_only or self._temporary_poll or not self.has_returned_state

    def pause(self):
        self._temporary_poll = True
        self._api.set_socketPersistent(False)
        if self._api.parent:
            self._api.parent.set_socketPersistent(False)

    def resume(self):
        self._temporary_poll = False

    async def async_receive(self):
        """Receive messages from a persistent connection asynchronously."""
        # If we didn't yet get any state from the device, we may need to
        # negotiate the protocol before making the connection persistent
        persist = not self.should_poll
        # flag to alternate updatedps and status calls to ensure we get
        # all dps updated
        dps_updated = False

        self._api.set_socketPersistent(persist)
        if self._api.parent:
            self._api.parent.set_socketPersistent(persist)

        last_heartbeat = self._cached_state.get("updated_at", 0)
        while self._running:
            error_count = self._api_working_protocol_failures
            force_backoff = False
            try:
                await self._api_lock.acquire()
                last_cache = self._cached_state.get("updated_at", 0)
                now = time()
                full_poll = False
                if persist == self.should_poll:
                    # use persistent connections after initial communication
                    # has been established.  Until then, we need to rotate
                    # the protocol version, which seems to require a fresh
                    # connection.
                    persist = not self.should_poll
                    _LOGGER.debug(
                        "%s persistant connection set to %s", self.name, persist
                    )
                    self._api.set_socketPersistent(persist)
                    if self._api.parent:
                        self._api.parent.set_socketPersistent(persist)
                    self._last_full_poll = 0  # ensure we start with a full poll

                needs_full_poll = now - self._last_full_poll > self._CACHE_TIMEOUT
                if now - last_cache > self._CACHE_TIMEOUT or (
                    persist and needs_full_poll
                ):
                    if (
                        self._force_dps
                        and not dps_updated
                        and self._api_protocol_working
                    ):
                        poll = await self._retry_on_failed_connection(
                            lambda: self._api.updatedps(self._force_dps),
                            f"Failed to update device dps for {self.name}",
                        )
                        dps_updated = True
                    else:
                        poll = await self._retry_on_failed_connection(
                            lambda: self._api.status(),
                            f"Failed to fetch device status for {self.name}",
                        )
                        dps_updated = False
                        full_poll = True
                    self._last_full_poll = now
                    last_heartbeat = now  # reset heartbeat timer on full poll
                elif persist:
                    if now - last_heartbeat > self._HEARTBEAT_INTERVAL:
                        await self._hass.async_add_executor_job(
                            self._api.heartbeat,
                            True,
                        )
                        last_heartbeat = now
                    poll = await self._hass.async_add_executor_job(
                        self._api.receive,
                    )
                    # Ignore Payload error 904, as 3.4 protocol devices seem to return
                    # this when there is no new data, instead of just returning nothing.
                    if poll and "Err" in poll and poll["Err"] == "904":
                        poll = None
                else:
                    force_backoff = True
                    poll = None

                if poll:
                    if "Error" in poll:
                        # increment the error count if not done already
                        if error_count == self._api_working_protocol_failures:
                            self._api_working_protocol_failures += 1
                        if self._api_working_protocol_failures == 1:
                            _LOGGER.warning(
                                "%s error reading: %s", self.name, poll["Error"]
                            )
                        else:
                            _LOGGER.debug(
                                "%s error reading: %s", self.name, poll["Error"]
                            )
                        if "Payload" in poll and poll["Payload"]:
                            _LOGGER.debug(
                                "%s err payload: %s",
                                self.name,
                                poll["Payload"],
                            )
                    else:
                        if "dps" in poll:
                            poll = poll["dps"]
                        if isinstance(poll, dict):
                            poll["full_poll"] = full_poll
                            yield poll

            except CancelledError:
                self._running = False
                # Close the persistent connection when exiting the loop
                persist = False
                self._api.set_socketPersistent(False)
                if self._api.parent:
                    self._api.parent.set_socketPersistent(False)
                raise
            except Exception as t:
                _LOGGER.exception(
                    "%s receive loop error %s:%s",
                    self.name,
                    type(t).__name__,
                    t,
                )
                persist = False
                self._api.set_socketPersistent(False)
                if self._api.parent:
                    self._api.parent.set_socketPersistent(False)
                force_backoff = True
            finally:
                if self._api_lock.locked():
                    self._api_lock.release()
            if not self.has_returned_state:
                force_backoff = True
            await asyncio.sleep(5 if force_backoff else 0.1)

        # Close the persistent connection when exiting the loop
        self._api.set_socketPersistent(False)
        if self._api.parent:
            self._api.parent.set_socketPersistent(False)

    def set_detected_product_id(self, product_id):
        self._product_ids.append(product_id)

    async def async_possible_types(self):
        cached_state = self._get_cached_state()
        if len(cached_state) <= 1:
            # in case of device22 devices, we need to poll them with a dp
            # that exists on the device to get anything back.  Most switch-like
            # devices have dp 1. Lights generally start from 20.  101 is where
            # vendor specific dps start.  Between them, these three should cover
            # most devices.  148 covers a doorbell device that didn't have these
            # 201 covers remote controllers and 2 and 9 cover others without 1
            self._api.set_dpsUsed(
                {
                    "1": None,
                    "2": None,
                    "9": None,
                    "20": None,
                    "60": None,
                    "101": None,
                    "148": None,
                    "201": None,
                }
            )
            await self.async_refresh()
            cached_state = self._get_cached_state()

        return await self._hass.async_add_executor_job(
            _collect_possible_matches,
            cached_state,
            self._product_ids,
        )

    async def async_inferred_type(self):
        best_match = None
        best_quality = 0
        cached_state = self._get_cached_state()
        possible = await self.async_possible_types()
        for config in possible:
            quality = config.match_quality(cached_state, self._product_ids)
            _LOGGER.info(
                "%s considering %s with quality %s",
                self.name,
                config.name,
                quality,
            )
            if quality > best_quality:
                best_quality = quality
                best_match = config

        if best_match:
            return best_match.config_type

        _LOGGER.warning(
            "Detection for %s with dps %s failed",
            self.name,
            log_json(cached_state),
        )

    async def async_refresh(self):
        _LOGGER.debug("Refreshing device state for %s", self.name)
        if not self._running:
            await self._retry_on_failed_connection(
                lambda: self._refresh_cached_state(),
                f"Failed to refresh device state for {self.name}.",
            )
        if self.dp102_persist_enabled:
            blob = self.get_property("102")
            if blob:
                self._dp102_verified_from_device = True
                self._schedule_persist_dp102(blob)

    def _normalize_dps(self, dps):
        return {str(key): value for key, value in dps.items()}

    def _poll_dps(self, poll):
        """String-keyed DP map from a receive-loop poll payload."""
        if not poll:
            return {}
        return self._normalize_dps(
            {k: v for k, v in poll.items() if str(k) != "full_poll"}
        )

    def _should_clear_dp_on_full_poll(self, dp_id, poll_dps):
        if self.dp102_persist_enabled and dp_id == "102":
            return False
        return dp_id not in poll_dps

    def _handle_dp102_poll(self, poll_dps):
        if not self.dp102_persist_enabled:
            return
        if "102" in poll_dps:
            self._dp102_verified_from_device = True
            self._dp102_pending_device_write = False
            self._schedule_persist_dp102(poll_dps["102"])
        elif "101" in poll_dps:
            self._schedule_dp102_on_101()

    def _clear_stale_dps_on_full_poll(self, poll_dps):
        for entity in self._children:
            for dp in entity._config.dps():
                if not dp.persist and self._should_clear_dp_on_full_poll(
                    dp.id, poll_dps
                ):
                    self._cached_state.pop(dp.id, None)

    def _merge_poll_into_cache(self, poll, full_poll=False):
        if not poll or "Err" in poll:
            return False
        dps = self._normalize_dps(poll.get("dps", {}))
        if not dps:
            return False
        self._cached_state = self._cached_state | dps
        self._cached_state["updated_at"] = time()
        self._handle_dp102_poll(dps)
        if full_poll:
            self._clear_stale_dps_on_full_poll(dps)
        for entity in self._children:
            entity.schedule_update_ha_state()
        return True

    def _combine_poll_responses(self, *polls):
        merged = {}
        for poll in polls:
            if poll and "Err" not in poll:
                merged.update(self._normalize_dps(poll.get("dps", {})))
        return {"dps": merged} if merged else None

    async def _pull_missing_force_dps(self):
        """Request missing force DPs, then a full status poll."""
        missing = self._missing_force_dps()
        if not missing:
            return None
        _LOGGER.debug("%s pulling missing dps %s", self.name, missing)
        upd = await self._retry_on_failed_connection(
            lambda: self._api.updatedps(missing),
            f"Failed to update device dps for {self.name}",
        )
        stat = await self._retry_on_failed_connection(
            lambda: self._api.status(),
            f"Failed to fetch device status for {self.name}",
        )
        return self._combine_poll_responses(upd, stat)

    def _missing_force_dps(self):
        return [
            dps_id
            for dps_id in self._force_dps
            if self.get_property(str(dps_id)) is None
        ]

    def _schedule_fetch_missing_force_dps(self):
        if not self._force_dps or not self._missing_force_dps():
            return
        if self._fetch_missing_scheduled:
            return
        self._fetch_missing_scheduled = True
        self._hass.async_create_task(self._async_fetch_missing_force_dps())

    async def _async_fetch_missing_force_dps(self):
        try:
            for _ in range(12):
                if not self._missing_force_dps():
                    return
                async with self._api_lock:
                    poll = await self._pull_missing_force_dps()
                    if poll:
                        self._merge_poll_into_cache(poll)
                if not self._missing_force_dps():
                    _LOGGER.info(
                        "%s fetched missing force dps",
                        self.name,
                    )
                    return
                await asyncio.sleep(5)
            remaining = self._missing_force_dps()
            if remaining:
                _LOGGER.warning(
                    "%s still missing dps after retries: %s",
                    self.name,
                    remaining,
                )
        finally:
            self._fetch_missing_scheduled = False

    async def _init_dp102_store(self):
        if self._dp102_store is not None:
            return
        self._dp102_store = Store(
            self._hass,
            DP102_STORE_VERSION,
            f"{DOMAIN}.dp102.{self.unique_id}",
        )
        if self._persisted_dp102 is None:
            stored = await self._dp102_store.async_load() or {}
            self._persisted_dp102 = stored.get("blob")

    def _schedule_persist_dp102(self, blob):
        if blob:
            self._hass.async_create_task(self._async_persist_dp102(blob))

    async def _async_persist_dp102(self, blob):
        """Save DP 102 blob when it changes (device poll or HA write)."""
        if not self.dp102_persist_enabled or not blob:
            return
        if blob == self._persisted_dp102:
            return
        await self._init_dp102_store()
        self._persisted_dp102 = blob
        await self._dp102_store.async_save({"blob": blob})
        _LOGGER.debug("%s persisted DP 102 settings blob", self.name)

    async def async_restore_dp102_to_cache(self):
        """Restore DP 102 from storage into cache for masked writes."""
        if not self.dp102_persist_enabled:
            return False
        blob = await self.async_load_dp102_cache()
        return bool(blob)

    def _apply_dp102_to_cache(self, blob, persist=False):
        if not blob:
            return None
        self._cached_state["102"] = blob
        self._cached_state["updated_at"] = time()
        for entity in self._children:
            entity.schedule_update_ha_state()
        if persist:
            self._hass.async_create_task(self._async_persist_dp102(blob))
        return blob

    def _mark_dp102_needs_device_write(self):
        if not self._dp102_verified_from_device and not self._dp102_session_written:
            self._dp102_pending_device_write = True

    def _schedule_dp102_on_101(self):
        """Device is online (101): ensure cache + write if needed."""
        if not self.dp102_persist_enabled or self._dp102_on_101_scheduled:
            return
        self._dp102_on_101_scheduled = True
        self._hass.async_create_task(self._async_handle_dp102_on_101())

    async def _async_fill_dp102_cache(self):
        """Build DP 102 cache from storage, device read, or synthesis."""
        if self._cached_state.get("102"):
            return True
        await self._init_dp102_store()
        if self._persisted_dp102:
            self._apply_dp102_to_cache(self._persisted_dp102)
            self._mark_dp102_needs_device_write()
            _LOGGER.info("%s: restored DP 102 cache from storage", self.name)
            return True
        await self.async_fetch_dps([102])
        if self._cached_state.get("102"):
            self._dp102_verified_from_device = True
            await self._async_persist_dp102(self._cached_state["102"])
            _LOGGER.info("%s: DP 102 read from device for cache", self.name)
            return True
        blob = synthesize_dp102_cache_blob(
            dp101_b64=self.get_property("101"),
        )
        self._apply_dp102_to_cache(blob, persist=False)
        self._mark_dp102_needs_device_write()
        _LOGGER.info(
            "%s: synthesized DP 102 cache (hold default, °C, sound off)",
            self.name,
        )
        return True

    async def _async_handle_dp102_on_101(self):
        """When 101 arrives: fill missing cache, then write once if needed."""
        try:
            if not self.dp102_persist_enabled or not self.get_property("101"):
                return
            if self._cached_state.get("102") and not self._dp102_pending_device_write:
                return
            if not self._cached_state.get("102"):
                await self._async_fill_dp102_cache()
            if self._dp102_pending_device_write and self._cached_state.get("102"):
                await self._async_push_dp102_to_device()
        finally:
            self._dp102_on_101_scheduled = False

    def _schedule_dp102_device_write(self):
        if (
            not self.dp102_persist_enabled
            or self._dp102_write_scheduled
            or not self._dp102_pending_device_write
            or not self._cached_state.get("102")
        ):
            return
        self._dp102_write_scheduled = True
        self._hass.async_create_task(self._async_push_dp102_to_device())

    async def _async_push_dp102_to_device(self):
        """Write cached DP 102 to the device once per session when needed."""
        try:
            if (
                not self.dp102_persist_enabled
                or not self._dp102_pending_device_write
            ):
                return False
            blob = self._cached_state.get("102")
            if not blob:
                return False
            if not self.get_property("101"):
                _LOGGER.debug(
                    "%s: deferring DP 102 write until device reports temps",
                    self.name,
                )
                return False
            _LOGGER.info("%s: writing DP 102 settings to device", self.name)
            async with self._api_lock:
                await self._retry_on_failed_connection(
                    lambda: self._set_values({"102": blob}),
                    f"Failed to push DP 102 settings to {self.name}",
                )
            if not self._cached_state.get("102"):
                return False
            self._dp102_pending_device_write = False
            self._dp102_session_written = True
            await self._async_persist_dp102(blob)
            for entity in self._children:
                entity.schedule_update_ha_state()
            _LOGGER.info("%s: DP 102 written to device", self.name)
            return True
        finally:
            self._dp102_write_scheduled = False

    async def async_load_dp102_cache(self):
        """Populate DP 102 in cache from storage (no device I/O)."""
        if not self.dp102_persist_enabled:
            return None
        try:
            await self._init_dp102_store()
        except Exception as err:
            _LOGGER.warning(
                "%s: DP 102 storage unavailable, using defaults: %s",
                self.name,
                err,
            )
        if self._dp102_verified_from_device and self.get_property("102"):
            return self.get_property("102")
        if self._persisted_dp102:
            self._apply_dp102_to_cache(self._persisted_dp102)
            self._mark_dp102_needs_device_write()
            return self._cached_state.get("102")
        blob = synthesize_dp102_cache_blob()
        self._apply_dp102_to_cache(blob)
        self._mark_dp102_needs_device_write()
        return blob

    def seed_dp102_cache_fallback(self):
        """Synchronous last-resort cache fill when async seed fails (no 101 needed)."""
        if not self.dp102_persist_enabled:
            return
        try:
            blob = self._persisted_dp102 or build_default_dp102_blob()
            self._cached_state["102"] = blob
            self._cached_state["updated_at"] = time()
            self._mark_dp102_needs_device_write()
        except Exception as err:
            _LOGGER.error("%s: could not build DP 102 fallback: %s", self.name, err)

    async def async_seed_dp102(self):
        """On startup: fill cache from storage; queue one write when device responds."""
        if not self.dp102_persist_enabled:
            return True

        await self.async_load_dp102_cache()
        if self._persisted_dp102:
            _LOGGER.info("%s: restored DP 102 cache from storage", self.name)
        elif self._cached_state.get("102"):
            _LOGGER.info("%s: seeded DP 102 cache (synthesized)", self.name)
        else:
            _LOGGER.warning("%s: DP 102 cache seed left cache empty", self.name)
        return True

    async def async_fetch_dps(self, dps_ids=None):
        """Fetch DPs from the device without sending a command."""
        ids = [int(dps_id) for dps_id in (dps_ids or self._force_dps or [])]
        _LOGGER.debug("Fetching dps %s for %s", ids, self.name)
        async with self._api_lock:
            if ids:
                upd = await self._retry_on_failed_connection(
                    lambda: self._api.updatedps(ids),
                    f"Failed to update device dps for {self.name}",
                )
                stat = await self._retry_on_failed_connection(
                    lambda: self._api.status(),
                    f"Failed to fetch device status for {self.name}",
                )
                poll = self._combine_poll_responses(upd, stat)
                if poll:
                    self._merge_poll_into_cache(poll)
            else:
                await self._retry_on_failed_connection(
                    lambda: self._refresh_cached_state(),
                    f"Failed to refresh device state for {self.name}.",
                )

    def get_property(self, dps_id):
        cached_state = self._get_cached_state()
        key = str(dps_id)
        return cached_state.get(key)

    async def async_set_property(self, dps_id, value):
        await self.async_set_properties({dps_id: value})

    def anticipate_property_value(self, dps_id, value):
        """
        Update a value in the cached state only. This is good for when you
        know the device will reflect a new state in the next update, but
        don't want to wait for that update for the device to represent
        this state.

        The anticipated value will be cleared with the next update.
        """
        self._cached_state[dps_id] = value

    def _reset_cached_state(self):
        dp102_blob = None
        if self.dp102_persist_enabled:
            dp102_blob = self._cached_state.get("102") or self._persisted_dp102
        self._cached_state = {"updated_at": 0}
        self._pending_updates = {}
        self._last_connection = 0
        self._last_full_poll = 0
        if dp102_blob:
            self._cached_state["102"] = dp102_blob
            self._cached_state["updated_at"] = time()
            if not self._dp102_verified_from_device and not self._dp102_session_written:
                self._dp102_pending_device_write = True

    def _refresh_cached_state(self):
        new_state = self._api.status()
        if new_state:
            if "Err" not in new_state:
                dps = self._normalize_dps(new_state.get("dps", {}))
                self._cached_state = self._cached_state | dps
                self._cached_state["updated_at"] = time()
                self._handle_dp102_poll(dps)
                self._clear_stale_dps_on_full_poll(dps)
                for entity in self._children:
                    entity.schedule_update_ha_state()
            elif self._api_working_protocol_failures == 1:
                _LOGGER.warning(
                    "%s protocol error %s: %s",
                    self.name,
                    new_state.get("Err"),
                    new_state.get("Error", "message not provided"),
                )
            else:
                _LOGGER.debug(
                    "%s protocol error %s: %s",
                    self.name,
                    new_state.get("Err"),
                    new_state.get("Error", "message not provided"),
                )
        _LOGGER.debug(
            "%s refreshed device state: %s",
            self.name,
            log_json(new_state),
        )
        _LOGGER.debug(
            "new state (incl pending): %s",
            log_json(self._get_cached_state()),
        )
        return new_state

    async def async_set_properties(self, properties):
        if len(properties) == 0:
            return

        self._add_properties_to_pending_updates(properties)
        await self._debounce_sending_updates()

    def _add_properties_to_pending_updates(self, properties):
        now = time()

        pending_updates = self._get_pending_updates()
        for key, value in properties.items():
            pending_updates[key] = {
                "value": value,
                "updated_at": now,
                "sent": False,
            }

        _LOGGER.debug(
            "%s new pending updates: %s",
            self.name,
            log_json(pending_updates),
        )

    def _remove_properties_from_pending_updates(self, data):
        self._pending_updates = {
            key: value
            for key, value in self._pending_updates.items()
            if key not in data or not value["sent"] or data[key] != value["value"]
        }

    async def _debounce_sending_updates(self):
        now = time()
        since = now - self._last_connection
        # set this now to avoid a race condition, it will be updated later
        # when the data is actally sent
        self._last_connection = now
        # Only delay a second if there was recently another command.
        # Otherwise delay 1ms, to keep things simple by reusing the
        # same send mechanism.
        waittime = 1 if since < 1.1 and self.should_poll else 0.001

        await asyncio.sleep(waittime)
        await self._send_pending_updates()

    async def _send_pending_updates(self):
        pending_properties = self._get_unsent_properties()

        _LOGGER.debug(
            "%s sending dps update: %s",
            self.name,
            log_json(pending_properties),
        )

        await self._retry_on_failed_connection(
            lambda: self._set_values(pending_properties),
            "Failed to update device state.",
        )
        if self.dp102_persist_enabled and "102" in pending_properties:
            self._schedule_persist_dp102(pending_properties["102"])
            self._dp102_pending_device_write = False
            self._dp102_session_written = True

    def _set_values(self, properties):
        try:
            self._lock.acquire()
            self._api.set_multiple_values(properties, nowait=True)
            self._cached_state["updated_at"] = 0
            now = time()
            self._last_connection = now
            pending_updates = self._get_pending_updates()
            for key in properties.keys():
                pending_updates[key]["updated_at"] = now
                pending_updates[key]["sent"] = True
        finally:
            self._lock.release()

    async def _retry_on_failed_connection(self, func, error_message):
        if self._api_protocol_version_index is None:
            await self._rotate_api_protocol_version()
        auto = (self._protocol_configured == "auto") and (
            not self._api_protocol_working
        )
        connections = (
            self._AUTO_CONNECTION_ATTEMPTS
            if auto
            else self._SINGLE_PROTO_CONNECTION_ATTEMPTS
        )

        last_err_code = None
        for i in range(connections):
            try:
                if not self._hass.is_stopping:
                    retval = await self._hass.async_add_executor_job(func)
                    if isinstance(retval, dict) and "Error" in retval:
                        last_err_code = retval.get("Err")
                        if last_err_code == "900":
                            # Some devices (e.g. IR/RF remotes) never return
                            # status data; error 900 is their normal response
                            # to a status query. Treat as reachable with no
                            # data so commands can still be sent.
                            self._cached_state["updated_at"] = time()
                            retval = None
                        else:
                            raise AttributeError(retval["Error"])
                    self._api_protocol_working = True
                    self._api_working_protocol_failures = 0
                    return retval
            except Exception as e:
                _LOGGER.debug(
                    "Retrying after exception %s %s (%d/%d)",
                    type(e).__name__,
                    e,
                    i,
                    connections,
                )
                # Ensure we have a fresh connection for the next attempt
                self._api.set_socketPersistent(False)
                if self._api.parent:
                    self._api.parent.set_socketPersistent(False)

                if i + 1 == connections:
                    self._reset_cached_state()
                    self._api_working_protocol_failures += 1
                    if (
                        self._api_working_protocol_failures
                        > self._AUTO_FAILURE_RESET_COUNT
                    ):
                        self._api_protocol_working = False
                        for entity in self._children:
                            entity.async_schedule_update_ha_state()
                    if self._api_working_protocol_failures == 1 and not (
                        last_err_code == "914" and self._protocol_configured == "auto"
                    ):
                        _LOGGER.error(error_message)
                    else:
                        _LOGGER.debug(error_message)

                if not self._api_protocol_working:
                    await self._rotate_api_protocol_version()

    def _get_cached_state(self):
        cached_state = self._cached_state.copy()
        return {**cached_state, **self._get_pending_properties()}

    def _get_pending_properties(self):
        return {key: prop["value"] for key, prop in self._get_pending_updates().items()}

    def _get_unsent_properties(self):
        return {
            key: info["value"]
            for key, info in self._get_pending_updates().items()
            if not info["sent"]
        }

    def _get_pending_updates(self):
        now = time()
        # sort pending updates according to their API identifier
        pending_updates_sorted = sorted(
            self._pending_updates.items(), key=lambda x: int(x[0])
        )
        self._pending_updates = {
            key: value
            for key, value in pending_updates_sorted
            if not value["sent"]
            or now - value.get("updated_at", 0) < self._FAKE_IT_TIMEOUT
        }
        return self._pending_updates

    async def _rotate_api_protocol_version(self):
        if self._api_protocol_version_index is None:
            try:
                self._api_protocol_version_index = API_PROTOCOL_VERSIONS.index(
                    self._protocol_configured
                )
            except ValueError:
                self._api_protocol_version_index = 0

        # only rotate if configured as auto
        elif self._protocol_configured == "auto":
            self._api_protocol_version_index += 1

        if self._api_protocol_version_index >= len(API_PROTOCOL_VERSIONS):
            self._api_protocol_version_index = 0

        new_version = API_PROTOCOL_VERSIONS[self._api_protocol_version_index]
        _LOGGER.debug(
            "Setting protocol version for %s to %s",
            self.name,
            new_version,
        )
        # Only enable tinytuya's auto-detect when using 3.22
        if new_version == 3.22:
            new_version = 3.3
            self._api.disabledetect = False
        else:
            self._api.disabledetect = True

        await self._hass.async_add_executor_job(
            self._api.set_version,
            new_version,
        )
        if self._api.parent:
            await self._hass.async_add_executor_job(
                self._api.parent.set_version,
                new_version,
            )

    @staticmethod
    def get_key_for_value(obj, value, fallback=None):
        keys = list(obj.keys())
        values = list(obj.values())
        return keys[values.index(value)] if value in values else fallback


def setup_device(hass: HomeAssistant, config: dict):
    """Setup a tuya device based on passed in config."""

    _LOGGER.info("Creating device: %s", get_device_id(config))
    hass.data[DOMAIN] = hass.data.get(DOMAIN, {})
    device = TuyaLocalDevice(
        config[CONF_NAME],
        config[CONF_DEVICE_ID],
        config[CONF_HOST],
        config[CONF_LOCAL_KEY],
        config[CONF_PROTOCOL_VERSION],
        config.get(CONF_DEVICE_CID),
        hass,
        config[CONF_POLL_ONLY],
        manufacturer=config.get(CONF_MANUFACTURER),
        model=config.get(CONF_MODEL),
    )
    hass.data[DOMAIN][get_device_id(config)] = {
        "device": device,
        "tuyadevice": device._api,
        "tuyadevicelock": device._api_lock,
    }

    return device


async def async_delete_device(hass: HomeAssistant, config: dict):
    device_id = get_device_id(config)
    _LOGGER.info("Deleting device: %s", device_id)
    await hass.data[DOMAIN][device_id]["device"].async_stop()
    del hass.data[DOMAIN][device_id]["device"]
    del hass.data[DOMAIN][device_id]["tuyadevice"]
    del hass.data[DOMAIN][device_id]["tuyadevicelock"]
