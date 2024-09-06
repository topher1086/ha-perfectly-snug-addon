import json
import yaml
from loguru import logger
import sys
import asyncio
import json
from datetime import datetime, timedelta, timezone
from time import time
from os import environ
import traceback
import pytz
import websockets
from dateutil.parser import parse as parse_dt

from requests import get

in_addon = not __file__.startswith("/workspaces/") and not __file__.startswith(
    "/home/chris/projects/"
)

try:
    with open("./data/options.json") as json_file:
        settings = json.load(json_file)
except FileNotFoundError:
    with open("./perfectly-snug/config.yaml") as yaml_file:
        full_config = yaml.safe_load(yaml_file)

    settings = full_config["options"]
    settings["topper_ip_address"] = "192.168.1.104"

try:
    with open("./perfectly-snug/test_config.yaml") as yaml_file:
        test_config = yaml.safe_load(yaml_file)
except FileNotFoundError:
    test_config = {}

update_interval_secs = settings['update_interval_secs']

# reset the logging level from DEBUG by default
logger.remove()
logger.add(sys.stderr, level=settings["logging_level"] if in_addon else "DEBUG")

base_url = "http://supervisor/core/api" if in_addon else test_config.get("base_url")

snuggler_url = f"ws://{settings['topper_ip_address']}/PSWS"

logger.debug(f"Base URL: {base_url}")

if in_addon:
    token = environ.get("SUPERVISOR_TOKEN")
else:
    token = test_config.get("token")

request_side = '{"Comm":"Status","sideID":"?","Val":"Side"}'
tx_id = 1

tz = "America/Chicago"


def now():
    utc_now = datetime.now(timezone.utc)
    return utc_now.astimezone(pytz.timezone(tz))

def now_naive():
    utc_now = datetime.now(timezone.utc)
    return utc_now.astimezone(pytz.timezone(tz)).replace(tzinfo=None)


def get_new_tx_id():
    global tx_id
    if tx_id >= 65535:
        tx_id = 1
    else:
        tx_id += 1
    return tx_id


class TopperState:
    def __init__(self) -> None:
        pass


class StatusHolder:
    def __init__(self, switch_type) -> None:
        self.switch_type = switch_type
        self.check_timestamp = now()
        self.last_mode_on = False

        if switch_type == "bedtime":
            self.last_weekday_time = now()
            self.last_weekend_time = now()

        else:
            logger.debug(f"{switch_type} created")

            self.overnight = {"R": {}, "L": {}}
            self.overnight = {"R": {}, "L": {}}

            self.schedule = {"R": {}, "L": {}}
            self.schedule = {"R": {}, "L": {}}

        # app is -10 -> +10
        # variables are 0 -> 20
        # -2 in the app is an 8
        # add 10 to the setting to make it correct for the api
        if switch_type == "playtime":
            self.run_level = int(settings["play_time_run_level"]) + 10
        elif switch_type == "naptime":
            self.run_level = int(settings["nap_time_run_level"]) + 10
        elif switch_type == "cool_off":
            self.run_level = int(settings["cool_off_run_level"]) + 10
        elif switch_type == "heat_up":
            self.run_level = int(settings["heat_up_run_level"]) + 10


status_switch_types = [
    'bedtime',
    'naptime',
    'playtime',
    'cool_off',
    'heat_up'
]

status_obj_list = [StatusHolder(switch_type=x) for x in status_switch_types]

# bedtime_status = StatusHolder(switch_type="bedtime")
# nap_time_status = StatusHolder(switch_type="naptime")
# play_time_status = StatusHolder(switch_type="playtime")
# cool_off_status = StatusHolder(switch_type="cool_off")
# heat_up_status = StatusHolder(switch_type="heat_up")


async def change_end_time(
    websocket,
    wake_up_time_dict,
    bedtime_mode_on,
    bedtime_mode_triggered,
    reset_schedule,
):
    logger.debug(f"Sending: {request_side}")
    await websocket.send(request_side)

    _ = await recv_msg(websocket)
    await asyncio.sleep(3)

    for side in ["R", "L"]:
        logger.debug(f"{side} side -- Checking settings")

        # check the current running status
        for _ in range(5):
            msg_s = {
                "Comm": "Status",
                "sideID": side,
                "Val": "Overnight",
                "TxId": get_new_tx_id(),
            }

            msg = json.dumps(msg_s, separators=(",", ":"))
            # logger.debug(f'Sending message: {msg}')

            await websocket.send(msg)

            # logger.debug('Msg sent')
            # await asyncio.sleep(2)

            overnight_msg = await recv_msg(websocket, "overnight")

            # logger.debug(f'Msg received: {overnight_msg}')

            if overnight_msg["sideID"] == side:
                break
            else:
                await asyncio.sleep(5)

        is_running = False
        if overnight_msg["Running"] == 1:
            logger.debug(f"{side} side -- is running")
            is_running = True

        # get the schedule settings now
        for _ in range(5):
            msg_s = {
                "Comm": "Status",
                "sideID": side,
                "Val": "Settings",
                "TxId": get_new_tx_id(),
            }

            msg = json.dumps(msg_s, separators=(",", ":"))
            # logger.debug(f'Sending message: {msg}')

            await websocket.send(msg)

            # logger.debug('Msg sent')
            # await asyncio.sleep(2)

            settings_msg = await recv_msg(websocket, "schedule")

            # logger.debug(f'Msg received: {settings_msg}')

            if settings_msg["sideID"] == side:
                break
            else:
                await asyncio.sleep(5)

        # copy the original message so we can compare the schedule easily
        orig_settings_msg = settings_msg.copy()

        # set the stop times for the schedules based on the Home Assistant setting
        settings_msg["Sched1StopH"] = str(wake_up_time_dict["weekday"].hour)
        settings_msg["Sched1StopM"] = str(wake_up_time_dict["weekday"].minute)

        settings_msg["Sched2StopH"] = str(wake_up_time_dict["weekend"].hour)
        settings_msg["Sched2StopM"] = str(wake_up_time_dict["weekend"].minute)

        # determine the actual starting date and time for the current time
        # if it's after midnight, go back a day to check the schedule
        # we'll just use 3am as the time since it would be unlikely this would change after that
        day_offset = 0
        if now_naive().hour <= 3:
            day_offset -= 1
        # this will be the index to get from the schedules
        # add since this will be a negative number
        schedule_day_index = now_naive().isoweekday() + day_offset

        # set the current schedule stop time
        weekday_or_weekend = (
            "weekday" if schedule_day_index in [1, 2, 3, 4, 5] else "weekend"
        )
        # go to tomorrow morning
        # if day_offset is -1, then it's morning and we shouldn't add a day, it would be today
        ha_stop_time = wake_up_time_dict[weekday_or_weekend] + timedelta(
            days=day_offset + 1
        )

        # if the index is 7, then it's Sunday and we need to change it to 0 for the first in the list
        if schedule_day_index == 7:
            schedule_day_index = 0

        current_start_time = None
        current_schedule = None
        # find the right schedule to look at the start time for
        for sched in [1, 2]:
            if settings_msg[f"Sched{sched}Day"][schedule_day_index] == 1:
                # subtract the day offset to get the correct start date and time
                current_start_time = parse_dt(
                    f"{settings_msg[f'Sched{sched}StartH']}:{settings_msg[f'Sched{sched}StartM']}"
                ) + timedelta(days=day_offset)
                current_schedule = sched
                break

        # if something didn't work or the day isn't scheduled, we'll quit
        if not current_start_time or not current_schedule:
            continue

        # figure out if the stop time changed for this schedule
        stop_time_changed = (
            orig_settings_msg[f"Sched{current_schedule}StopH"]
            != settings_msg[f"Sched{current_schedule}StopH"]
            or orig_settings_msg[f"Sched{current_schedule}StopM"]
            != settings_msg[f"Sched{current_schedule}StopM"]
        )

        # flag if it's passed the start time now and the topper should be running
        past_start_time = (current_start_time - now_naive()).total_seconds() < 0
        # past_pre_heat_start_time = (current_start_time - now_naive()).total_seconds() < 3600

        # figure out if the ending schedule should be updated
        update_endtime_schedule = False

        # stop and restart if wake up time changed
        stop_and_restart = False

        # before start time - just update the schedule
        if not past_start_time and stop_time_changed:
            logger.info(
                f"{side} side -- Before start time and scheduled changed, just updating it"
            )
            update_endtime_schedule = True

        elif past_start_time and stop_time_changed:
            if (ha_stop_time - now_naive()).total_seconds() < 7500:
                logger.info(
                    f"{side} side -- Past start time, but within 7500 seconds of end time, do nothing"
                )
            else:
                logger.info(
                    f"{side} side -- Past start time, updating schedule and restarting topper"
                )
                update_endtime_schedule = True
                stop_and_restart = True

        elif (
            past_start_time
            and bedtime_mode_on
            and bedtime_mode_triggered
            and not is_running
        ):
            logger.info(
                f"{side} side -- Past start time and bedtime just started and topper is not running"
            )
            if (ha_stop_time - now_naive()).total_seconds() < 7500:
                logger.info(
                    f"{side} side -- Past start time, but within 7500 seconds of end time, do nothing"
                )
            else:
                logger.info(f"{side} side -- Past start time, starting topper")
                stop_and_restart = True

        elif reset_schedule:
            logger.info(f"{now().strftime('%H:%M')} -- Time to reset start times")
            # time to reset the start time
            settings_msg["Sched1StartH"] = str(
                parse_dt(settings["weeknight_start_time"]).hour
            )
            settings_msg["Sched1StartM"] = str(
                parse_dt(settings["weeknight_start_time"]).minute
            )

            settings_msg["Sched2StartH"] = str(
                parse_dt(settings["weekendnight_start_time"]).hour
            )
            settings_msg["Sched2StartM"] = str(
                parse_dt(settings["weekendnight_start_time"]).minute
            )

            update_endtime_schedule = True

        # update settings if needed
        if update_endtime_schedule:
            logger.info(f"{side} side -- Sending message to update schedule")
            msg = json.dumps(settings_msg, separators=(",", ":"))
            # logger.debug(f'Sending message: {msg}')

            await websocket.send(msg)
            await asyncio.sleep(5)

        if stop_and_restart:
            logger.info(f"{side} side -- Stop and restart topper")

            for _ in range(5):
                msg_s = {
                    "Comm": "Status",
                    "sideID": side,
                    "Val": "Overnight",
                    "TxId": get_new_tx_id(),
                }

                msg = json.dumps(msg_s, separators=(",", ":"))
                # logger.debug(f'Sending message: {msg}')

                await websocket.send(msg)

                # logger.debug('Msg sent')
                # await asyncio.sleep(2)

                overnight_msg = await recv_msg(websocket, "overnight")

                # logger.debug(f'Msg received: {overnight_msg}')

                if overnight_msg["sideID"] == side:
                    break
                else:
                    await asyncio.sleep(5)

            if overnight_msg["Running"] == 1:
                logger.info(
                    f"{side} side -- Currently running, sending message to stop it"
                )

                overnight_msg["Running"] = 0
                overnight_msg["TxId"] = get_new_tx_id()

                msg = json.dumps(overnight_msg, separators=(",", ":"))
                # logger.debug(f'Sending message: {msg}')

                await websocket.send(msg)
                await asyncio.sleep(5)

            # now start the topper
            logger.info(f"{side} side -- Sending message to start topper")

            overnight_msg["Running"] = 1
            overnight_msg["TxId"] = get_new_tx_id()

            msg = json.dumps(overnight_msg, separators=(",", ":"))
            # logger.debug(f'Sending message: {msg}')

            await websocket.send(msg)
            await asyncio.sleep(2)


async def mode_changed_update_topper(
    websocket: websockets.WebSocketClientProtocol,
    status_obj: StatusHolder,
    mode_on: bool,
):
    logger.info(f'{status_obj.switch_type} changed to {"ON" if mode_on else "OFF"}')

    logger.debug(f"Sending: {request_side}")
    await websocket.send(request_side)

    _ = await recv_msg(websocket)
    await asyncio.sleep(3)

    if mode_on:
        for side in ["R", "L"]:
            logger.info(
                f"{side} side -- Getting settings to update and starting topper"
            )

            # check the current running status
            overnight_success = False
            for _ in range(10):
                msg_s = {
                    "Comm": "Status",
                    "sideID": side,
                    "Val": "Overnight",
                    "TxId": get_new_tx_id(),
                }

                msg = json.dumps(msg_s, separators=(",", ":"))
                # logger.debug(f'Sending message: {msg}')

                await websocket.send(msg)

                # logger.debug('Msg sent')
                # await asyncio.sleep(2)

                overnight_msg = await recv_msg(websocket, "overnight")

                # logger.debug(f'Msg received: {overnight_msg}')

                if overnight_msg["sideID"] == side:
                    overnight_success = True
                    break
                else:
                    await asyncio.sleep(5)

            if overnight_msg["Running"] == 1:
                logger.warning(f"{side} side is running, skipping")
                break

            if not overnight_success:
                logger.warning(
                    f"Could not get overnight status for {side} side, skipping"
                )
                break

            # set the status in the object to revert back to
            status_obj.overnight[side] = overnight_msg.copy()

            # get the schedule settings now
            schedule_success = False
            for _ in range(10):
                msg_s = {
                    "Comm": "Status",
                    "sideID": side,
                    "Val": "Settings",
                    "TxId": get_new_tx_id(),
                }

                msg = json.dumps(msg_s, separators=(",", ":"))
                # logger.debug(f'Sending message: {msg}')

                await websocket.send(msg)

                # logger.debug('Msg sent')
                # await asyncio.sleep(2)

                schedule_msg = await recv_msg(websocket, "schedule")

                # logger.debug(f'Msg received: {schedule_msg}')

                if schedule_msg["sideID"] == side:
                    schedule_success = True
                    break
                else:
                    await asyncio.sleep(5)

            if not schedule_success:
                logger.warning(
                    f"Could not get schedule status for {side} side, skipping"
                )
                break

            # set the status in the object to revert back to
            status_obj.schedule[side] = schedule_msg.copy()

            # update the schedule now
            logger.info(f"{side} side -- Sending message to update schedule time")

            t1_time = 1
            if status_obj.switch_type in ['cool_off', 'heat_up']:
                t1_time = 90

            schedule_msg["T1"] = t1_time
            schedule_msg["TxId"] = get_new_tx_id()

            msg = json.dumps(schedule_msg, separators=(",", ":"))

            await websocket.send(msg)
            await asyncio.sleep(1)

            logger.info(f"{side} side -- Sending message to update levels")

            # set to -2 level for now ########################
            overnight_msg["Running"] = 1
            overnight_msg["L1"] = status_obj.run_level
            overnight_msg["L2"] = status_obj.run_level
            overnight_msg["L3"] = status_obj.run_level
            overnight_msg["TxId"] = get_new_tx_id()

            msg = json.dumps(overnight_msg, separators=(",", ":"))

            await websocket.send(msg)
            await asyncio.sleep(1)

    else:
        for side in ["R", "L"]:
            logger.info(f"{side} side -- Resetting settings and stopping topper")

            # update the schedule now
            logger.info(f"{side} side -- Sending message to update schedule time")

            schedule_msg = status_obj.schedule[side].copy()
            schedule_msg["TxId"] = get_new_tx_id()

            msg = json.dumps(schedule_msg, separators=(",", ":"))

            await websocket.send(msg)
            await asyncio.sleep(1)

            logger.info(f"{side} side -- Sending message to update levels")

            overnight_msg = status_obj.overnight[side].copy()
            overnight_msg["Running"] = 0
            overnight_msg["TxId"] = get_new_tx_id()

            msg = json.dumps(overnight_msg, separators=(",", ":"))

            await websocket.send(msg)
            await asyncio.sleep(1)


def check_msg(m, m_type=None):
    if m_type is None:
        return True

    if type(m) != dict:
        return False

    if m_type == "schedule":
        good = True
        if m.get("Comm") != "Set":
            good = False
        if m.get("Sched1StopH") is None:
            good = False
        if m.get("Sched1StopM") is None:
            good = False
        if m.get("Sched2StopH") is None:
            good = False
        if m.get("Sched2StopM") is None:
            good = False
        if m.get("sideID") is None:
            good = False

        return good

    if m_type == "overnight":
        good = True
        if m.get("Comm") != "Overnight":
            good = False
        if m.get("L1") is None:
            good = False
        if m.get("L2") is None:
            good = False
        if m.get("L3") is None:
            good = False
        if m.get("Running") is None:
            good = False
        if m.get("sideID") is None:
            good = False

        return good

    return False


async def recv_msg(websocket, m_type=None):
    for _ in range(10):
        try:
            # logger.debug('Waiting for websocket message')
            # logger.debug(f'Websocket state: {websocket.state}')
            # logger.debug(f'WS type: {type(websocket)}')
            # logger.debug(f'WS recv type: {type(websocket.recv())}')
            # m = await asyncio.wait_for(websocket.recv(), timeout=5)
            m = await websocket.recv()
            last_msg_rec = json.loads(m)
            logger.debug(f"Last msg received: {last_msg_rec}")

            if check_msg(last_msg_rec, m_type):
                return last_msg_rec

        except Exception as e:
            logger.warning(f"Exception getting websocket message: {type(e)} -- {e}")


async def get_wake_up_times():
    weekday_url = f"{base_url}/states/{settings['weekday_wake_up_time_helper']}"
    weekend_url = f"{base_url}/states/{settings['weekend_wake_up_time_helper']}"

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    weekday_r = get(weekday_url, headers=headers)
    weekend_r = get(weekend_url, headers=headers)

    weekday_j = weekday_r.json()
    weekend_j = weekend_r.json()

    return {
        "weekday": parse_dt(weekday_j["state"]),
        "weekend": parse_dt(weekend_j["state"]),
        "weekday_j": weekday_j,
        "weekend_j": weekend_j,
    }


# async def get_bedtime_mode():

#     url = f"{base_url}/states/{settings['bedtime_mode_helper']}"

#     headers = {
#         'Authorization': f'Bearer {token}',
#         'Content-Type': 'application/json',
#     }

#     r = get(url, headers=headers)

#     return r.json()


async def get_switch_status(entity_id):
    url = f"{base_url}/states/{entity_id}"

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    r = get(url, headers=headers)

    return r.json()


async def check_switch_for_change(logger_prepend: str, switch_entity_id: str, status_obj: StatusHolder, delay_off_secs: int):
    switch_status = await get_switch_status(switch_entity_id)

    switch_on = switch_status["state"] == "on"

    status_obj.check_timestamp = now()

    if not status_obj.last_mode_on and switch_on:
        logger.info(
            f"{logger_prepend} has been turned on, updating topper settings and starting it"
        )
        status_obj.last_mode_on = switch_on

        async with websockets.connect(snuggler_url) as ws:
            await asyncio.gather(
                mode_changed_update_topper(
                    ws, status_obj, status_obj.last_mode_on
                )
            )

    elif (
        status_obj.last_mode_on and not switch_on
    ):
        logger.info(
            f"{logger_prepend} has been turned off and delay over, reverting topper settings and stopping it"
        )
        status_obj.last_mode_on = switch_on

        sleep_time = max(parse_dt(switch_status["last_changed"]).timestamp() + delay_off_secs - time(), 0)
        if sleep_time > 0:
            logger.info(f'{logger_prepend} turned off, but need to delay {sleep_time:,.2f} seconds before changing topper')
            await asyncio.sleep(sleep_time)

        async with websockets.connect(snuggler_url) as ws:
            await asyncio.gather(
                mode_changed_update_topper(
                    ws, status_obj, status_obj.last_mode_on
                )
            )


async def snuggler_update():
    reset_start_time_sec_window = int(
        (update_interval_secs * 3) + (update_interval_secs / 2)
    )

    while True:

        try:
            async with websockets.connect(base_url.replace('http://', 'ws://') + '/websocket') as ha_ws:

                connected_to_ws = False
                # connect and receive the first message
                connect_response = await ha_ws.recv()

                auth_response = None
                if json.loads(connect_response)['type'] == 'auth_required':

                    # Send authentication message
                    auth_data = json.dumps({"type": "auth", "access_token": token})
                    await ha_ws.send(auth_data)

                    # Receive response (hopefully successful authentication)
                    auth_response = await ha_ws.recv()
                    json_auth_response = json.loads(auth_response)

                    if json_auth_response['type'] == 'auth_ok':
                        connected_to_ws = True
                        logger.info("Connected to Home Assistant Websocket API")

                if not connected_to_ws:
                    logger.critical(f"Unable to authenticate to HA: {auth_response}")
                    raise ConnectionError(f"Unabled to authenticate to HA: {auth_response}")

                subscribe_events_data = {
                    'id': get_new_tx_id(),
                    'type': 'subscribe_events',
                    'event_type': 'state_changed'
                }
                await ha_ws.send(json.dumps(subscribe_events_data))
                sub_resp = await ha_ws.recv()
                sub_resp_data = json.loads(sub_resp)
                if not sub_resp_data['success']:
                    logger.critical(f"Unable to subscribe to state_changed events: {sub_resp}")
                    raise ConnectionError(f"Unable to subscribe to state_changed events: {sub_resp}")

                st = time() - 999999
                while ha_ws.open:
                    try:

                        msg = await ha_ws.recv()
                        msg_json = json.loads(msg)

                        same_state = msg_json['event']['data']['old_state']['state'] == msg_json['event']['data']['new_state']['state']

                        entity_id = msg_json['event']['data']['entity_id']

                        if not same_state and entity_id == settings["bedtime_mode_helper"]:
                            logger.info('Bedtime mode status changed')

                        elif not same_state and entity_id == settings["play_time_mode_helper"]:
                            logger.info('Playtime mode status changed')

                        elif not same_state and entity_id == settings["nap_time_mode_helper"]:
                            logger.info('Naptime mode status changed')

                        elif not same_state and entity_id == settings["cool_off_topper_helper"]:
                            logger.info('Cool off topper status changed')

                        elif not same_state and entity_id == settings["heat_up_topper_helper"]:
                            logger.info('Heat up topper status changed')

                        elif not same_state and entity_id == settings["weekday_wake_up_time_helper"]:
                            logger.info('Weekday wake up time changed')
                            # wait for a bit before processing in case we weren't done changing the time
                            await asyncio.sleep(15)

                        elif not same_state and entity_id == settings["weekend_wake_up_time_helper"]:
                            logger.info('Weekend wake up time changed')
                            # wait for a bit before processing in case we weren't done changing the time
                            await asyncio.sleep(15)

                        elif st + update_interval_secs < time():
                            logger.info(f'Update interval of {update_interval_secs:,} seconds passed, checking status')

                        elif not same_state:
                            logger.debug(f"State of entity changed: {entity_id} -- {msg_json['event']['data']['old_state']['state']} -> {msg_json['event']['data']['new_state']['state']}")
                            continue
                        else:
                            # just an attribute change
                            continue

                        logger.debug("Checking bedtime status...")

                        bedtime_status = [x for x in status_obj_list if x.switch_type == 'bedtime'][0]

                        last_weekday_time = bedtime_status.last_weekday_time
                        last_weekend_time = bedtime_status.last_weekend_time

                        bedtime_mode = await get_switch_status(settings["bedtime_mode_helper"])

                        bedtime_mode_triggered = (
                            now() - parse_dt(bedtime_mode["last_changed"])
                        ).total_seconds() < update_interval_secs + (
                            update_interval_secs / 2
                        )
                        bedtime_mode_on = bedtime_mode["state"] == "on"

                        wake_up_time_dict = await get_wake_up_times()

                        bedtime_status.check_timestamp = now()
                        bedtime_status.last_weekday_time = wake_up_time_dict["weekday"]
                        bedtime_status.last_weekend_time = wake_up_time_dict["weekend"]
                        bedtime_status.last_mode_on = bedtime_mode_on

                        # check if the wake up time has been updated
                        need_to_update = False
                        reset_schedule = False

                        if (
                            last_weekday_time.hour != wake_up_time_dict["weekday"].hour
                            or last_weekday_time.minute != wake_up_time_dict["weekday"].minute
                        ):  # noqa: E501
                            logger.info(
                                f"Weekday wakeup time has been changed: {last_weekday_time} to {wake_up_time_dict['weekday']}"
                            )
                            need_to_update = True

                        elif (
                            last_weekend_time.hour != wake_up_time_dict["weekend"].hour
                            or last_weekend_time.minute != wake_up_time_dict["weekend"].minute
                        ):  # noqa: E501
                            logger.info(
                                f"Weekend wakeup time has been changed: {last_weekend_time} to {wake_up_time_dict['weekend']}"
                            )
                            need_to_update = True

                        else:
                            # if the topper is set to heat, it will start that 1 hour before the "start" time
                            # weeknight would be Sunday->Thursday nights
                            if now().isoweekday() in [7, 1, 2, 3, 4]:
                                start_time = settings["weeknight_start_time"]
                            else:
                                start_time = settings["weekendnight_start_time"]

                            # get the window to reset the start times
                            reset_start_time_window_start = pytz.timezone(tz).localize(
                                parse_dt(start_time)
                            ) - timedelta(hours=1, seconds=reset_start_time_sec_window)
                            reset_start_time_window_end = pytz.timezone(tz).localize(
                                parse_dt(start_time)
                            ) - timedelta(hours=1, seconds=15)

                            if (
                                now() >= reset_start_time_window_start
                                and now() <= reset_start_time_window_end
                            ):
                                logger.info("Time to reset start times")
                                need_to_update = True
                                reset_schedule = True

                        if need_to_update:
                            async with websockets.connect(snuggler_url) as ws:
                                await asyncio.gather(
                                    change_end_time(
                                        ws,
                                        wake_up_time_dict,
                                        bedtime_mode_on,
                                        bedtime_mode_triggered,
                                        reset_schedule,
                                    )
                                )

                        else:
                            logger.debug("Wakeup times have not changed, not updating")

                    except Exception as e:
                        logger.critical(traceback.print_exc())
                        logger.critical(f"Exception: {type(e)}: {e}")

                    # check nap time now
                    try:
                        switch_type = 'naptime'
                        logger_prepend = 'Nap time mode'
                        switch_entity_id = settings["nap_time_mode_helper"]

                        delay_off_secs = 100

                        logger.debug(f"Checking {switch_type} status...")

                        something_else_on = False
                        for s in status_obj_list:
                            if s.switch_type == switch_type:
                                continue
                            if s.last_mode_on == True:
                                logger.info(f'{s.switch_type.capitalize()} is on, not checking {switch_type}')
                                something_else_on = True

                        if not something_else_on:
                            status_obj = [x for x in status_obj_list if x.switch_type == switch_type][0]

                            await check_switch_for_change(
                                logger_prepend=logger_prepend,
                                switch_entity_id=switch_entity_id,
                                status_obj=status_obj,
                                delay_off_secs=delay_off_secs
                            )

                    except Exception as e:
                        logger.critical(traceback.print_exc())
                        logger.critical(f"Exception: {type(e)}: {e}")

                    # check play time now
                    try:
                        switch_type = 'playtime'
                        logger_prepend = 'Play time mode'
                        switch_entity_id = settings["play_time_mode_helper"]

                        delay_off_secs = 100

                        logger.debug(f"Checking {switch_type} status...")

                        something_else_on = False
                        for s in status_obj_list:
                            if s.switch_type == switch_type:
                                continue
                            if s.last_mode_on == True:
                                logger.info(f'{s.switch_type.capitalize()} is on, not checking {switch_type}')
                                something_else_on = True

                        if not something_else_on:
                            status_obj = [x for x in status_obj_list if x.switch_type == switch_type][0]

                            await check_switch_for_change(
                                logger_prepend=logger_prepend,
                                switch_entity_id=switch_entity_id,
                                status_obj=status_obj,
                                delay_off_secs=delay_off_secs
                            )

                    except Exception as e:
                        logger.critical(traceback.print_exc())
                        logger.critical(f"Exception: {type(e)}: {e}")

                    # check cool off topper
                    try:
                        switch_type = 'cool_off'
                        logger_prepend = 'Cool off topper'
                        switch_entity_id = settings["cool_off_topper_helper"]

                        delay_off_secs = 0

                        logger.debug(f"Checking {switch_type} status...")

                        something_else_on = False
                        for s in status_obj_list:
                            if s.switch_type == switch_type:
                                continue
                            if s.last_mode_on == True:
                                logger.info(f'{s.switch_type.capitalize()} is on, not checking {switch_type}')
                                something_else_on = True

                        if not something_else_on:
                            status_obj = [x for x in status_obj_list if x.switch_type == switch_type][0]

                            await check_switch_for_change(
                                logger_prepend=logger_prepend,
                                switch_entity_id=switch_entity_id,
                                status_obj=status_obj,
                                delay_off_secs=delay_off_secs
                            )

                    except Exception as e:
                        logger.critical(traceback.print_exc())
                        logger.critical(f"Exception: {type(e)}: {e}")

                    # check heat up topper
                    try:
                        switch_type = 'heat_up'
                        logger_prepend = 'Heat up topper'
                        switch_entity_id = settings["heat_up_topper_helper"]

                        delay_off_secs = 0

                        logger.debug(f"Checking {switch_type} status...")

                        something_else_on = False
                        for s in status_obj_list:
                            if s.switch_type == switch_type:
                                continue
                            if s.last_mode_on == True:
                                logger.info(f'{s.switch_type.capitalize()} is on, not checking {switch_type}')
                                something_else_on = True

                        if not something_else_on:
                            status_obj = [x for x in status_obj_list if x.switch_type == switch_type][0]

                            await check_switch_for_change(
                                logger_prepend=logger_prepend,
                                switch_entity_id=switch_entity_id,
                                status_obj=status_obj,
                                delay_off_secs=delay_off_secs
                            )

                    except Exception as e:
                        logger.critical(traceback.print_exc())
                        logger.critical(f"Exception: {type(e)}: {e}")

                    if now().minute == 0:
                        logger.info(
                            f"Waiting {update_interval_secs:,} seconds to check again..."
                        )
                    else:
                        logger.debug(
                            f"Waiting {update_interval_secs:,} seconds to check again..."
                        )

                    logger.debug(f"Time taken to check: {(time() - st) * 1000:,.0f} ms")
                    # await asyncio.sleep(update_interval_secs)
                    st = time()

        except Exception as e:
            logger.critical(traceback.print_exc())
            logger.critical(f"Exception: {type(e)}: {e}")
            await asyncio.sleep(60)


if __name__ == "__main__":
    asyncio.run(snuggler_update())
