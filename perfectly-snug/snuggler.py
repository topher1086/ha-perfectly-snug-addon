import json
import yaml
from loguru import logger
import sys
import asyncio
import json
from datetime import datetime, timedelta
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

logger.debug(f'Base URL: {base_url}')

if in_addon:
    token = environ.get('SUPERVISOR_TOKEN')
else:
    token = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiIxNGVhYzhiNjdmOTM0MDZmODYwNmFkOTcwOGJlNDgzOSIsImlhdCI6MTY5OTgyODUwNiwiZXhwIjoyMDE1MTg4NTA2fQ.q9jEO7QIpgSpUYd-uhqxtYkBolq8kJI1xzf2bJvl_fA'

request_side = '{"Comm":"Status","sideID":"?","Val":"Side"}'
tx_id = 1

def now():  # sourcery skip: aware-datetime-for-utc
    utc_now = pytz.utc.localize(datetime.utcnow())
    return utc_now.astimezone(pytz.timezone('America/Chicago'))


def get_new_tx_id():
    global tx_id
    if tx_id >= 65535:
        tx_id = 1
    else:
        tx_id += 1
    return tx_id




async def change_end_time(websocket, wake_up_time_dict, bedtime_mode_on, bedtime_mode_triggered):

    logger.debug(f'Sending: {request_side}')
    await websocket.send(request_side)

    r_msg = await recv_msg(websocket)
    await asyncio.sleep(3)

    for side in ['R', 'L']:

        logger.debug(f'Side {side} -- Checking settings')

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
            logger.debug(f'Side {side} -- is running')
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
            logger.warning(f'Side {side} -- Bedtime mode is on AND triggered recently AND the topper is NOT running')
            update_start_time = True

        if orig_r_msg != r_msg and bedtime_mode_on:
            logger.info(f'Side {side} -- Change in settings with bedtime mode on, updating start times')
            update_start_time = True

        if orig_r_msg != r_msg and is_running:
            logger.info(f'Side {side} -- Change in settings with the topper running, updating start times')
            update_start_time = True

        if update_start_time:
            logger.info(f'Side {side} -- Updating start times')

            start_time = now() + timedelta(minutes=2)

            r_msg['Sched1StartH'] = str(start_time.hour)
            r_msg['Sched1StartM'] = str(start_time.minute)

            r_msg['Sched2StartH'] = str(start_time.hour)
            r_msg['Sched2StartM'] = str(start_time.minute)

        # check if it's time to reset the start schedule
        if not update_start_time and orig_r_msg == r_msg and now().hour == 16 and now().minute < 14:
            logger.info(f"{now().strftime('%H:%M')} -- Time to reset start times")
            # time to reset the start time
            r_msg['Sched1StartH'] = str(parse_dt(settings['weeknight_start_time']).hour)
            r_msg['Sched1StartM'] = str(parse_dt(settings['weeknight_start_time']).minute)

            r_msg['Sched2StartH'] = str(parse_dt(settings['weekendnight_start_time']).hour)
            r_msg['Sched2StartM'] = str(parse_dt(settings['weekendnight_start_time']).minute)

            update_start_time = True

        if orig_r_msg != r_msg:
            logger.info(f'Side {side} -- Sending message to update schedule')
            msg = json.dumps(r_msg, separators=(',', ':'))
            # logger.debug(f'Sending message: {msg}')

            await websocket.send(msg)
            await asyncio.sleep(5)

        if update_start_time:
            logger.info(f'Side {side} -- Update start time and it is running, stopping it')

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
                logger.info(f'Side {side} running and schedule changed, stopping it')
                r_msg['Running'] = 0

                msg = json.dumps(r_msg, separators=(',', ':'))
                # logger.debug(f'Sending message: {msg}')

                await websocket.send(msg)
                await asyncio.sleep(5)


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

async def get_bedtime_mode():

    url = f"{base_url}/states/{settings['bedtime_mode_helper']}"

    headers = {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
    }

    r = get(url, headers=headers)

    return r.json()



async def snuggler_update():

    # last_run_file_name = 'last_run_status.json'

    snuggler_url = f"ws://{settings['topper_ip_address']}/PSWS"

    # logger.debug(f'Snuggler instruction: {instruction}')

    # get the last run time
    # try:
    #     with open(last_run_file_name) as f:
    #         last_run_status = json.load(f)
    # except FileNotFoundError:
    last_run_status = {
        'timestamp': now(),
        'last_weekday_time': now(),
        'last_weekend_time': now(),
        'last_bedtime_mode_on': False
    }

    while True:

        try:

            logger.debug('Checking status...')

            # last_run_time = parse_dt(last_run_status['timestamp'])
            last_weekday_time = last_run_status['last_weekday_time']
            last_weekend_time = last_run_status['last_weekend_time']
            # last_bedtime_mode_on = last_run_status['last_bedtime_mode_on']

            bedtime_mode = await get_bedtime_mode()

            bedtime_mode_triggered = (now() - parse_dt(bedtime_mode['last_changed'])).total_seconds() < settings['update_interval_secs'] + (settings['update_interval_secs'] / 2)
            bedtime_mode_on = bedtime_mode['state'] == 'on'

            # bedtime_mode_last_changed = parse_dt(max(bedtime_mode['last_changed'], bedtime_mode['last_updated']))

            wake_up_time_dict = await get_wake_up_times()
            # logger.debug(f'Wake up time dict: {wake_up_time_dict}')

            # weekday_wake_up_time_last_changed = parse_dt(max(wake_up_time_dict['weekday_j']['last_changed'], wake_up_time_dict['weekday_j']['last_updated']))  # noqa: E501
            # weekend_wake_up_time_last_changed = parse_dt(max(wake_up_time_dict['weekend_j']['last_changed'], wake_up_time_dict['weekend_j']['last_updated']))  # noqa: E501

            # wake_up_last_changed = max(weekday_wake_up_time_last_changed, weekend_wake_up_time_last_changed)

            # set the timestamp now
            last_run_status |= {
                'timestamp': now(),
                'last_weekday_time': wake_up_time_dict['weekday'],
                'last_weekend_time': wake_up_time_dict['weekend'],
                'last_bedtime_mode_on': bedtime_mode_on,
            }

            # check if the wake up time has been updated
            need_to_update = False
            if last_weekday_time.hour != wake_up_time_dict['weekday'].hour or last_weekday_time.minute != wake_up_time_dict['weekday'].minute:  # noqa: E501
                logger.info(f"Weekday wakeup time has been changed: {last_weekday_time} to {wake_up_time_dict['weekday']}")
                need_to_update = True

            elif last_weekend_time.hour != wake_up_time_dict['weekend'].hour or last_weekend_time.minute != wake_up_time_dict['weekend'].minute:  # noqa: E501
                logger.info(f"Weekend wakeup time has been changed: {last_weekend_time} to {wake_up_time_dict['weekend']}")
                need_to_update = True

            elif now().hour == 16 and now().minute < settings['update_interval_secs'] * 3:
                logger.info('Time to reset start times')
                need_to_update = True

            if need_to_update:
                async with websockets.connect(snuggler_url) as ws:

                    # await asyncio.gather(change_end_time(ws, parsed_time), receive(ws))
                    await asyncio.gather(change_end_time(ws, wake_up_time_dict, bedtime_mode_on, bedtime_mode_triggered))

            else:
                logger.debug('Wakeup times have not changed, not updating')

            # with open(last_run_file_name, 'w') as f:
            #     json.dump(last_run_status, f)

        except Exception as e:
            logger.critical(traceback.print_exc())
            logger.critical(f'Exception: {type(e)}: {e}')

        logger.info(f"Waiting {settings['update_interval_secs']:,} seconds to check again...")
        await asyncio.sleep(settings['update_interval_secs'])


if __name__ == "__main__":
    asyncio.run(snuggler_update())


