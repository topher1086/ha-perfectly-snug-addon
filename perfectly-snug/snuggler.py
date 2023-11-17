import json
import yaml
from loguru import logger
import sys
import asyncio
import json
from datetime import datetime, timedelta
from time import time
from os import environ
import traceback
import pytz
import websockets
from dateutil.parser import parse as parse_dt

from requests import get

in_addon = not __file__.startswith('/workspaces/')

try:
    with open('./data/options.json') as json_file:
        settings = json.load(json_file)
except FileNotFoundError:
    with open('./perfectly-snug/config.yaml') as yaml_file:
        full_config = yaml.safe_load(yaml_file)

    settings = full_config['options']
    settings['topper_ip_address'] = '192.168.1.104'

# reset the logging level from DEBUG by default
logger.remove()
logger.add(sys.stderr, level=settings['logging_level'] if in_addon else 'DEBUG')

base_url = 'http://supervisor/core/api' if in_addon else 'http://localhost:8123/api'

snuggler_url = f"ws://{settings['topper_ip_address']}/PSWS"

logger.debug(f'Base URL: {base_url}')

if in_addon:
    token = environ.get('SUPERVISOR_TOKEN')
else:
    token = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJkYTMzYmFkY2UwMWE0OTk5ODJhOTlkMjljMmQzM2JhMiIsImlhdCI6MTcwMDE3MjUwOCwiZXhwIjoyMDE1NTMyNTA4fQ.FMQsbWDIOp3yXhSyXCP9IQhrkLW-UGxiskGrt9szjWU'

request_side = '{"Comm":"Status","sideID":"?","Val":"Side"}'
tx_id = 1

tz = 'America/Chicago'


def now():  # sourcery skip: aware-datetime-for-utc
    utc_now = pytz.utc.localize(datetime.utcnow())
    return utc_now.astimezone(pytz.timezone(tz))


def get_new_tx_id():
    global tx_id
    if tx_id >= 65535:
        tx_id = 1
    else:
        tx_id += 1
    return tx_id


class TopperState():

    def __init__(self) -> None:
        pass

class StatusHolder():

    def __init__(self, switch_type) -> None:

        self.switch_type = switch_type
        self.check_timestamp = now()
        self.last_mode_on = False

        if switch_type == 'bedtime':
            self.last_weekday_time = now()
            self.last_weekend_time = now()

        else:
            logger.debug(f'{switch_type} created')

            self.overnight = {'R': {}, 'L': {}}
            self.overnight = {'R': {}, 'L': {}}

            self.schedule = {'R': {}, 'L': {}}
            self.schedule = {'R': {}, 'L': {}}

        # app is -10 -> +10
        # variables are 0 -> 20
        # -2 in the app is an 8
        # add 10 to the setting to make it correct for the api
        if switch_type == 'playtime':
            self.run_level = int(settings['play_time_run_level']) + 10
        elif switch_type == 'naptime':
            self.run_level = int(settings['nap_time_run_level']) + 10

bedtime_status = StatusHolder(switch_type='bedtime')
nap_time_status = StatusHolder(switch_type='naptime')
play_time_status = StatusHolder(switch_type='playtime')



async def change_end_time(websocket, wake_up_time_dict, bedtime_mode_on, bedtime_mode_triggered, reset_start_times):

    logger.debug(f'Sending: {request_side}')
    await websocket.send(request_side)

    r_msg = await recv_msg(websocket)
    await asyncio.sleep(3)

    for side in ['R', 'L']:

        logger.debug(f'{side} side -- Checking settings')

        # check the current running status
        for _ in range(5):

            msg_s = {
                "Comm": "Status",
                "sideID": side,
                "Val": "Overnight",
                "TxId": get_new_tx_id()
            }

            msg = json.dumps(msg_s, separators=(',', ':'))
            # logger.debug(f'Sending message: {msg}')

            await websocket.send(msg)

            # logger.debug('Msg sent')
            # await asyncio.sleep(2)

            r_msg = await recv_msg(websocket, 'overnight')

            # logger.debug(f'Msg received: {r_msg}')

            if r_msg['sideID'] == side:
                break
            else:
                await asyncio.sleep(5)

        is_running = False
        if r_msg['Running'] == 1:
            logger.debug(f'{side} side -- is running')
            is_running = True

        # get the schedule settings now
        for _ in range(5):

            msg_s = {
                "Comm": "Status",
                "sideID": side,
                "Val": "Settings",
                "TxId": get_new_tx_id()
            }

            msg = json.dumps(msg_s, separators=(',', ':'))
            # logger.debug(f'Sending message: {msg}')

            await websocket.send(msg)

            # logger.debug('Msg sent')
            # await asyncio.sleep(2)

            r_msg = await recv_msg(websocket, 'schedule')

            # logger.debug(f'Msg received: {r_msg}')

            if r_msg['sideID'] == side:
                break
            else:
                await asyncio.sleep(5)

        orig_r_msg = r_msg.copy()

        r_msg['Sched1StopH'] = str(wake_up_time_dict['weekday'].hour)
        r_msg['Sched1StopM'] = str(wake_up_time_dict['weekday'].minute)

        r_msg['Sched2StopH'] = str(wake_up_time_dict['weekend'].hour)
        r_msg['Sched2StopM'] = str(wake_up_time_dict['weekend'].minute)

        # figure out if the start time should be updated
        update_start_time = False

        # this will be regardless of whether the stop time changed or not
        # if bedtime mode has been turned on and the topper isn't running
        # then the start time was probably too late and it should be kicked off now
        if bedtime_mode_on and bedtime_mode_triggered and not is_running:
            logger.warning(f'{side} side -- Bedtime mode is on AND triggered recently AND the topper is NOT running')
            update_start_time = True

        if orig_r_msg != r_msg and bedtime_mode_on:
            logger.info(f'{side} side -- Change in settings with bedtime mode on, updating start times')
            update_start_time = True

        if orig_r_msg != r_msg and is_running:
            logger.info(f'{side} side -- Change in settings with the topper running, updating start times')
            update_start_time = True

        if update_start_time:
            logger.info(f'{side} side -- Updating start times')

            start_time = now() + timedelta(minutes=2)

            r_msg['Sched1StartH'] = str(start_time.hour)
            r_msg['Sched1StartM'] = str(start_time.minute)

            r_msg['Sched2StartH'] = str(start_time.hour)
            r_msg['Sched2StartM'] = str(start_time.minute)

        # check if it's time to reset the start schedule
        if not update_start_time and reset_start_times:
            logger.info(f"{now().strftime('%H:%M')} -- Time to reset start times")
            # time to reset the start time
            r_msg['Sched1StartH'] = str(parse_dt(settings['weeknight_start_time']).hour)
            r_msg['Sched1StartM'] = str(parse_dt(settings['weeknight_start_time']).minute)

            r_msg['Sched2StartH'] = str(parse_dt(settings['weekendnight_start_time']).hour)
            r_msg['Sched2StartM'] = str(parse_dt(settings['weekendnight_start_time']).minute)

            update_start_time = True

        if orig_r_msg != r_msg:
            logger.info(f'{side} side -- Sending message to update schedule')
            msg = json.dumps(r_msg, separators=(',', ':'))
            # logger.debug(f'Sending message: {msg}')

            await websocket.send(msg)
            await asyncio.sleep(5)

        if update_start_time:
            logger.info(f'{side} side -- Update start time and it is running, stopping it')

            for _ in range(5):

                msg_s = {
                    "Comm": "Status",
                    "sideID": side,
                    "Val": "Overnight",
                    "TxId": get_new_tx_id()
                }

                msg = json.dumps(msg_s, separators=(',', ':'))
                # logger.debug(f'Sending message: {msg}')

                await websocket.send(msg)

                # logger.debug('Msg sent')
                # await asyncio.sleep(2)

                r_msg = await recv_msg(websocket, 'overnight')

                # logger.debug(f'Msg received: {r_msg}')

                if r_msg['sideID'] == side:
                    break
                else:
                    await asyncio.sleep(5)

            if r_msg['Running'] == 1:
                logger.info(f'{side} side running and schedule changed, stopping it')
                r_msg['Running'] = 0

                msg = json.dumps(r_msg, separators=(',', ':'))
                # logger.debug(f'Sending message: {msg}')

                await websocket.send(msg)
                await asyncio.sleep(5)


async def mode_changed_update_topper(websocket: websockets.WebSocketClientProtocol, status_obj: StatusHolder, mode_on: bool):

    logger.info(f'{status_obj.switch_type} changed to {"ON" if mode_on else "OFF"}')


    logger.debug(f'Sending: {request_side}')
    await websocket.send(request_side)

    _ = await recv_msg(websocket)
    await asyncio.sleep(3)

    if mode_on:
        for side in ['R', 'L']:

            logger.info(f'{side} side -- Getting settings to update and starting topper')

            # check the current running status
            overnight_success = False
            for _ in range(10):

                msg_s = {
                    "Comm": "Status",
                    "sideID": side,
                    "Val": "Overnight",
                    "TxId": get_new_tx_id()
                }

                msg = json.dumps(msg_s, separators=(',', ':'))
                # logger.debug(f'Sending message: {msg}')

                await websocket.send(msg)

                # logger.debug('Msg sent')
                # await asyncio.sleep(2)

                overnight_msg = await recv_msg(websocket, 'overnight')

                # logger.debug(f'Msg received: {overnight_msg}')

                if overnight_msg['sideID'] == side:
                    overnight_success = True
                    break
                else:
                    await asyncio.sleep(5)

            if overnight_msg['Running'] == 1:
                logger.warning(f'{side} side is running, skipping')
                break

            if not overnight_success:
                logger.warning(f'Could not get overnight status for {side} side, skipping')
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
                    "TxId": get_new_tx_id()
                }

                msg = json.dumps(msg_s, separators=(',', ':'))
                # logger.debug(f'Sending message: {msg}')

                await websocket.send(msg)

                # logger.debug('Msg sent')
                # await asyncio.sleep(2)

                schedule_msg = await recv_msg(websocket, 'schedule')

                # logger.debug(f'Msg received: {schedule_msg}')

                if schedule_msg['sideID'] == side:
                    schedule_success = True
                    break
                else:
                    await asyncio.sleep(5)

            if not schedule_success:
                logger.warning(f'Could not get schedule status for {side} side, skipping')
                break

            # set the status in the object to revert back to
            status_obj.schedule[side] = schedule_msg.copy()

            # update the schedule now
            logger.info(f'{side} side -- Sending message to update schedule time')

            schedule_msg['T1'] = 1
            schedule_msg['TxId'] = get_new_tx_id()

            msg = json.dumps(schedule_msg, separators=(',', ':'))

            await websocket.send(msg)
            await asyncio.sleep(1)

            logger.info(f'{side} side -- Sending message to update levels')

            # set to -2 level for now ########################
            overnight_msg['Running'] = 1
            overnight_msg['L1'] = status_obj.run_level
            overnight_msg['L2'] = status_obj.run_level
            overnight_msg['L3'] = status_obj.run_level
            overnight_msg['TxId'] = get_new_tx_id()

            msg = json.dumps(overnight_msg, separators=(',', ':'))

            await websocket.send(msg)
            await asyncio.sleep(1)

    else:

        for side in ['R', 'L']:

            logger.info(f'{side} side -- Resetting settings and stopping topper')

            # update the schedule now
            logger.info(f'{side} side -- Sending message to update schedule time')

            schedule_msg = status_obj.schedule[side].copy()
            schedule_msg['TxId'] = get_new_tx_id()

            msg = json.dumps(schedule_msg, separators=(',', ':'))

            await websocket.send(msg)
            await asyncio.sleep(1)

            logger.info(f'{side} side -- Sending message to update levels')

            overnight_msg = status_obj.overnight[side].copy()
            overnight_msg['Running'] = 0
            overnight_msg['TxId'] = get_new_tx_id()

            msg = json.dumps(overnight_msg, separators=(',', ':'))

            await websocket.send(msg)
            await asyncio.sleep(1)


def check_msg(m, m_type=None):

    if m_type is None:
        return True

    if type(m) != dict:
        return False

    if m_type == 'schedule':
        good = True
        if m.get('Comm') != 'Set':
            good = False
        if m.get('Sched1StopH') is None:
            good = False
        if m.get('Sched1StopM') is None:
            good = False
        if m.get('Sched2StopH') is None:
            good = False
        if m.get('Sched2StopM') is None:
            good = False
        if m.get('sideID') is None:
            good = False

        return good

    if m_type == 'overnight':
        good = True
        if m.get('Comm') != 'Overnight':
            good = False
        if m.get('L1') is None:
            good = False
        if m.get('L2') is None:
            good = False
        if m.get('L3') is None:
            good = False
        if m.get('Running') is None:
            good = False
        if m.get('sideID') is None:
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
            logger.debug(f'Last msg received: {last_msg_rec}')

            if check_msg(last_msg_rec, m_type):
                return last_msg_rec

        except Exception as e:
            logger.warning(f'Exception getting websocket message: {type(e)} -- {e}')


async def get_wake_up_times():

    weekday_url = f"{base_url}/states/{settings['weekday_wake_up_time_helper']}"
    weekend_url = f"{base_url}/states/{settings['weekend_wake_up_time_helper']}"

    headers = {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
    }

    weekday_r = get(weekday_url, headers=headers)
    weekend_r = get(weekend_url, headers=headers)

    weekday_j = weekday_r.json()
    weekend_j = weekend_r.json()

    return {
        'weekday': parse_dt(weekday_j['state']),
        'weekend': parse_dt(weekend_j['state']),
        'weekday_j': weekday_j,
        'weekend_j': weekend_j
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
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
    }

    r = get(url, headers=headers)

    return r.json()

async def snuggler_update():

    reset_start_time_sec_window = int((settings['update_interval_secs'] * 3) + (settings['update_interval_secs'] / 2))

    while True:

        try:

            st = time()
            logger.debug('Checking bedtime status...')

            last_weekday_time = bedtime_status.last_weekday_time
            last_weekend_time = bedtime_status.last_weekend_time

            bedtime_mode = await get_switch_status(settings['bedtime_mode_helper'])

            bedtime_mode_triggered = (now() - parse_dt(bedtime_mode['last_changed'])).total_seconds() < settings['update_interval_secs'] + (settings['update_interval_secs'] / 2)
            bedtime_mode_on = bedtime_mode['state'] == 'on'

            wake_up_time_dict = await get_wake_up_times()

            bedtime_status.check_timestamp = now()
            bedtime_status.last_weekday_time = wake_up_time_dict['weekday']
            bedtime_status.last_weekend_time = wake_up_time_dict['weekend']
            bedtime_status.last_mode_on = bedtime_mode_on

            # check if the wake up time has been updated
            need_to_update = False
            reset_start_times = False

            if last_weekday_time.hour != wake_up_time_dict['weekday'].hour or last_weekday_time.minute != wake_up_time_dict['weekday'].minute:  # noqa: E501
                logger.info(f"Weekday wakeup time has been changed: {last_weekday_time} to {wake_up_time_dict['weekday']}")
                need_to_update = True

            elif last_weekend_time.hour != wake_up_time_dict['weekend'].hour or last_weekend_time.minute != wake_up_time_dict['weekend'].minute:  # noqa: E501
                logger.info(f"Weekend wakeup time has been changed: {last_weekend_time} to {wake_up_time_dict['weekend']}")
                need_to_update = True

            else:
                # if the topper is set to heat, it will start that 1 hour before the "start" time
                # weeknight would be Sunday->Thursday nights
                if now().isoweekday() in [7, 1, 2, 3, 4]:
                    start_time = settings['weeknight_start_time']
                else:
                    start_time = settings['weekendnight_start_time']

                # get the window to reset the start times
                reset_start_time_window_start = pytz.timezone(tz).localize(parse_dt(start_time)) - timedelta(hours=1, seconds=reset_start_time_sec_window)
                reset_start_time_window_end = pytz.timezone(tz).localize(parse_dt(start_time)) - timedelta(hours=1, seconds=15)

                if now() >= reset_start_time_window_start and now() <= reset_start_time_window_end:
                    logger.info('Time to reset start times')
                    need_to_update = True
                    reset_start_times = True

            if need_to_update:
                async with websockets.connect(snuggler_url) as ws:

                    await asyncio.gather(change_end_time(ws, wake_up_time_dict, bedtime_mode_on, bedtime_mode_triggered, reset_start_times))

            else:
                logger.debug('Wakeup times have not changed, not updating')

        except Exception as e:
            logger.critical(traceback.print_exc())
            logger.critical(f'Exception: {type(e)}: {e}')

        # check nap time now
        try:

            logger.debug('Checking nap time status...')

            if bedtime_status.last_mode_on:
                logger.info('Bedtime mode is on so not checking nap time')

            elif play_time_status.last_mode_on:
                logger.info('Play time mode is on so not checking nap time')

            else:

                nap_time_off_delay_secs = 100

                status_obj = nap_time_status

                nap_mode = await get_switch_status(settings['nap_time_mode_helper'])

                nap_mode_delay_over = (now() - parse_dt(nap_mode['last_changed'])).total_seconds() > nap_time_off_delay_secs
                nap_mode_on = nap_mode['state'] == 'on'

                status_obj.check_timestamp = now()

                if not status_obj.last_mode_on and nap_mode_on:
                    logger.info('Nap mode has been turned on, updating topper settings and starting it')
                    status_obj.last_mode_on = nap_mode_on

                    async with websockets.connect(snuggler_url) as ws:
                        await asyncio.gather(mode_changed_update_topper(ws, status_obj, status_obj.last_mode_on))

                elif status_obj.last_mode_on and not nap_mode_on and nap_mode_delay_over:
                    logger.info('Nap mode has been turned off and delay over, reverting topper settings and stopping it')
                    status_obj.last_mode_on = nap_mode_on

                    async with websockets.connect(snuggler_url) as ws:
                        await asyncio.gather(mode_changed_update_topper(ws, status_obj, status_obj.last_mode_on))

                elif status_obj.last_mode_on and not nap_mode_on:
                    # don't update the object status since we didn't turn it off yet
                    logger.info(f'Nap mode has been turned off, but delay off of {nap_time_off_delay_secs:,} seconds not over yet')


        except Exception as e:
            logger.critical(traceback.print_exc())
            logger.critical(f'Exception: {type(e)}: {e}')

        # check play time now
        try:

            logger.debug('Checking play time status...')

            if bedtime_status.last_mode_on:
                logger.info('Bedtime mode is on so not checking play time')

            elif nap_time_status.last_mode_on:
                logger.info('Play time mode is on so not checking play time')

            else:

                play_time_off_delay_secs = 300

                status_obj = play_time_status

                play_mode = await get_switch_status(settings['play_time_mode_helper'])

                play_mode_delay_over = (now() - parse_dt(play_mode['last_changed'])).total_seconds() > play_time_off_delay_secs
                play_mode_on = play_mode['state'] == 'on'

                status_obj.check_timestamp = now()

                if not status_obj.last_mode_on and play_mode_on:
                    logger.info('Play mode has been turned on, updating topper settings and starting it')
                    status_obj.last_mode_on = play_mode_on

                    async with websockets.connect(snuggler_url) as ws:
                        await asyncio.gather(mode_changed_update_topper(ws, status_obj, status_obj.last_mode_on))

                elif status_obj.last_mode_on and not play_mode_on and play_mode_delay_over:
                    logger.info('Play mode has been turned off and delay over, reverting topper settings and stopping it')
                    status_obj.last_mode_on = play_mode_on

                    async with websockets.connect(snuggler_url) as ws:
                        await asyncio.gather(mode_changed_update_topper(ws, status_obj, status_obj.last_mode_on))

                elif status_obj.last_mode_on and not play_mode_on:
                    # don't update the object status since we didn't turn it off yet
                    logger.info(f'Play mode has been turned off, but delay off of {play_time_off_delay_secs:,} seconds not over yet')


        except Exception as e:
            logger.critical(traceback.print_exc())
            logger.critical(f'Exception: {type(e)}: {e}')


        logger.info(f"Waiting {settings['update_interval_secs']:,} seconds to check again...")

        logger.debug(f'Time taken to check: {(time() - st) * 1000:,.0f} ms')
        await asyncio.sleep(settings['update_interval_secs'])


if __name__ == "__main__":
    asyncio.run(snuggler_update())


