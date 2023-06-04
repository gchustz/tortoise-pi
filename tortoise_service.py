import RPi.GPIO as GPIO
import glob
import time
import datetime
import pytz
import sys
import os
import math 

# TODO: Change directory, file, and path operations to pathlib
# TODO: Change non-global values to normal snake case 

# globals
RELAY_CLOSED = GPIO.LOW
RELAY_OPEN = GPIO.HIGH

RELAY_CHANNELS = {
    1:26, # UV
    2:20, # HEATLAMP 1
    3:21  # HEATLAMP 2
}

with open('/etc/timezone', 'r') as tzf:
    TZ = tzf.read().strip().replace('\n', '')

TZ = pytz.timezone(TZ)
THERMOMETER_PATH = glob.glob('/sys/bus/w1/devices/28*/w1_slave')[-1]

# Functions
def get_HHMM_time(dt) -> int:
    return int(dt.strftime('%H%M'))

def setup() -> None:

    GPIO.setmode(GPIO.BCM)

    # Set channels as output
    for channel in RELAY_CHANNELS.values():
        GPIO.setup(channel, GPIO.OUT)

def poll_thermometer() -> float:
    with open(THERMOMETER_PATH, 'r') as thermometer:
        dat = thermometer.read().split()[-1]
        t = float(dat.split('=')[-1]) / 1000

        return t
    
def purge_old_files(purge_period, directory, filename_delim='_') -> None:
    # Calculate the purge threshold
    purge_threshold = time.time() - purge_period 
        
    # Assuming that the file names are of form
    # *<filename_delim><time int>.<file_ext>
    if directory[-1] == '/':
        directory = directory[:-1]
    
    files = [file for file in os.listdir(directory) if os.path.isfile('/'.join([directory, file]))]

    for file in files:
        cand = file.split('.')[0]
        cand = cand.split(filename_delim)[-1]
        cand = int(cand)

        if cand <= purge_threshold: 
            filepath = '/'.join([directory, file])
            
            try:
                os.remove(filepath)
            
            except KeyboardInterrupt:
                print('Keyboard interrupt during purge, try again')
                return
            
            except Exception as e:
                # Do not want to kill the program on a file rotation issue
                print(e)

# Main class
class EnvControl:
    # Constants
    RELAY_CYCLE_PERIOD = 10
    UV_GPIO_CHANNEL = RELAY_CHANNELS[1]
    HEAT_LAMP_GPIO_CHANNELS = [
        RELAY_CHANNELS[2],
        RELAY_CHANNELS[3]
    ]

    def __init__(self, args): 

        self.period = args.period
        self.curr_datetime = datetime.datetime.now(TZ)
        self.curr_time = time.time()
        self.HHMM_time = get_HHMM_time(self.curr_datetime)
        self.curr_temp = poll_thermometer()
        self.heartbeat = 0

        # UV
        self.UV_CMD_STATE = RELAY_OPEN
        self.UV_GPIO_STATE = None
        self.UV_TURN_ON_TIME = args.DAY_START_HHMM
        self.UV_TURN_OFF_TIME = args.NIGHT_START_HHMM
        # TODO: change to day/night 

        # heat
        self.HEAT_CMD_STATE = RELAY_OPEN
        self.HEAT_GPIO_STATE = None 
        self.TEMP_MAX_NOMINAL = args.TEMP_DAYTIME
        self.TEMP_MIN_NOMINAL = args.TEMP_NIGHTTIME
        self.TEMP_TOLERANCE = args.TEMP_CONTROL_BOUNDS

        assert self.TEMP_MAX_NOMINAL >= self.TEMP_MIN_NOMINAL

        self.TEMP_AVG = (self.TEMP_MAX_NOMINAL + self.TEMP_MIN_NOMINAL) / 2
        self.TEMP_SWING = (self.TEMP_MAX_NOMINAL - self.TEMP_MIN_NOMINAL) / 2
        self.OFFSET_TIME = self.UV_TURN_ON_TIME // 100 
        # Using per hour temperature to avoid heat lamp rapid switching
        
        self.HEAT_CRIT_LOW = args.TEMP_LOW_CRITICAL
        self.HEAT_CRIT_HIGH = args.TEMP_HIGH_CRITICAL
        assert self.HEAT_CRIT_LOW < self.TEMP_MIN_NOMINAL - self.TEMP_TOLERANCE < self.TEMP_MAX_NOMINAL + self.TEMP_TOLERANCE < self.HEAT_CRIT_HIGH
        
        # File rotation parameters
        self.file_rotation_period = args.file_rotation_period
        self.file_deletion_period = args.file_deletion_period

        # io
        self.linuxiodir = args.linuxiodir
        if self.linuxiodir[-1] == '/':
            self.linuxiodir = self.linuxiodir[:-1]
        assert os.path.isdir(self.linuxiodir)

        # data
        self.datadir = args.datadir
        if self.datadir[-1] == '/':
            self.datadir = self.datadir[:-1]
        assert os.path.isdir(self.datadir)

        self.new_data_file()

        # log
        self.logdir = args.logdir

        if self.logdir[-1] == '/':
            self.logdir = self.logdir[:-1]
        assert os.path.isdir(self.logdir)
        
        self.new_log_file()
        
        # looping
        self.iterations = 0

        # Cycle the relay twice on first start
        self.cycle_relays()
    
    def new_data_file(self):
        purge_old_files(self.file_deletion_period, self.datadir)

        self.curr_data_time = time.time()
        self.datapath = f'{self.datadir}/tortoise_gpio_{int(self.curr_data_time)}.csv'

        self.append_data('iter', 'datetime', 'curr_temp', 'heat_state', 'curr_time', 'uv_state')

    def new_log_file(self):
        purge_old_files(self.file_deletion_period, self.logdir)

        self.curr_log_time = time.time()

        self.logpath = f'{self.logdir}/tortoise_gpio_{int(self.curr_log_time)}.log'

    def update(self):

        # Cycle relays every couple of hours and on the first start
        if self.iterations % (4*3600) == 0: # Note iterations are not an exact second
            self.cycle_relays()
        
        # Times
        self.curr_datetime = datetime.datetime.now(TZ)
        self.curr_time = time.time()
        self.heartbeat = int(not self.heartbeat)

        # File rotation
        if self.curr_data_time <= self.curr_time - self.file_rotation_period:
            self.new_data_file()
        
        if self.curr_log_time <= self.curr_time - self.file_rotation_period:
            self.new_log_file()

        # Measurements
        self.HHMM_time = get_HHMM_time(self.curr_datetime)
        self.curr_temp = poll_thermometer()

        # UV
        if self.UV_CMD_STATE == RELAY_OPEN and (self.UV_TURN_ON_TIME <= self.HHMM_time < self.UV_TURN_OFF_TIME):
            self.UV_CMD_STATE = RELAY_CLOSED
        
        elif self.UV_CMD_STATE == RELAY_CLOSED and (self.HHMM_time < self.UV_TURN_ON_TIME or self.UV_TURN_OFF_TIME <= self.HHMM_time):
            self.UV_CMD_STATE = RELAY_OPEN
        
        if self.UV_GPIO_STATE is None or self.UV_CMD_STATE != self.UV_GPIO_STATE:
            GPIO.output(self.UV_GPIO_CHANNEL, self.UV_CMD_STATE)
            self.UV_GPIO_STATE = self.UV_CMD_STATE

        # Temp
        hr = self.HHMM_time // 100
        self.NOMINAL_TEMP = self.TEMP_SWING * math.sin((hr-self.OFFSET_TIME)/12*math.pi) + self.TEMP_AVG
        
        self.HEAT_TURN_ON_TEMP = self.NOMINAL_TEMP + self.TEMP_TOLERANCE
        self.HEAT_TURN_OFF_TEMP = self.NOMINAL_TEMP - self.TEMP_TOLERANCE

        if self.HEAT_CMD_STATE == RELAY_OPEN and (self.curr_temp <= self.HEAT_TURN_ON_TEMP):
            self.HEAT_CMD_STATE = RELAY_CLOSED

        elif self.HEAT_CMD_STATE == RELAY_CLOSED and (self.HEAT_TURN_OFF_TEMP <= self.curr_temp):
            self.HEAT_CMD_STATE = RELAY_OPEN

        if self.HEAT_GPIO_STATE is None or self.HEAT_CMD_STATE != self.HEAT_GPIO_STATE:
            for heat_channel in self.HEAT_LAMP_GPIO_CHANNELS:
                GPIO.output(heat_channel, self.HEAT_CMD_STATE)

            self.HEAT_GPIO_STATE = self.HEAT_CMD_STATE

        # TODO: add heatlamp switching when there's multiple heatlamps

        # TODO: add timeout for heat lamp to prevent rapid switching
        
        # TODO: add alarms  for heat

        self.push_linuxio(heartbeat=self.heartbeat, logpath=self.logpath, datapath=self.datapath, iter=self.iterations, temp=self.curr_temp, uv_light=self.UV_GPIO_STATE, heat_light=self.HEAT_GPIO_STATE)

        self.append_data(self.iterations, self.curr_datetime, self.curr_temp, self.HEAT_CMD_STATE, self.HHMM_time, self.UV_CMD_STATE)
        
        self.log()
        self.iterations += 1

    def push_linuxio(self, **data_points):
        for filename, value in data_points.items():
            filename = '/'.join([self.linuxiodir, filename])
            with open(filename, 'w') as linuxfile:
                linuxfile.write(str(value))

    def append_data(self, *data_points):

        if self.HEAT_GPIO_STATE is None or self.HEAT_CMD_STATE != self.HEAT_GPIO_STATE:
            for heat_channel in self.HEAT_LAMP_GPIO_CHANNELS:
                GPIO.output(heat_channel, self.HEAT_CMD_STATE)

        data_points = [str(d) for d in data_points]
        with open(self.datapath, 'a') as datafile:
            datafile.write(','.join(data_points) + '\n')
            
    def log(self, *lines):
        log_str = f'{self.iterations} {self.curr_datetime}: | CLOSED RELAY={RELAY_CLOSED} '

        temp_state_str = f'| TEMP {self.HEAT_CRIT_LOW}/{self.NOMINAL_TEMP - self.TEMP_TOLERANCE}/{self.curr_temp}/{self.NOMINAL_TEMP + self.TEMP_TOLERANCE}/{self.HEAT_CRIT_HIGH} C -> [{self.HEAT_GPIO_STATE}/{self.HEAT_CMD_STATE}] '
        log_str += temp_state_str

        uv_state_str = f'| UV {self.UV_TURN_ON_TIME}/{self.HHMM_time}/{self.UV_TURN_OFF_TIME} -> [{self.UV_GPIO_STATE}/{self.UV_CMD_STATE}] '
        log_str += uv_state_str

        for line in lines:
            log_str += '\nERROR: ' + str(line)

        log_str += '\n'

        with open(self.logpath, 'a') as logf:
            logf.write(log_str)

    def cycle_relays(self):
        for channel in [self.UV_GPIO_CHANNEL, *self.HEAT_LAMP_GPIO_CHANNELS]:
            self.log(f'Cycling Relay GPIO Channel {channel}')
            GPIO.output(channel, RELAY_OPEN)
            time.sleep(self.RELAY_CYCLE_PERIOD)
            GPIO.output(channel, RELAY_CLOSED)
            time.sleep(self.RELAY_CYCLE_PERIOD)
            GPIO.output(channel, RELAY_OPEN)

        self.UV_GPIO_STATE = RELAY_OPEN
        self.HEAT_GPIO_STATE = RELAY_OPEN

    def loop(self):
        # Add kernel interrupt handling for graceful shutdown
        try:
            while True:
                self.update()
                time.sleep(self.period)

        except KeyboardInterrupt:
            print('\nKBI!')
            return 0

        except Exception as e:
            self.log(e)
            return 1

# Main loop
def main(args):
    setup()

    envcontrol = EnvControl(args)

    exit_code = envcontrol.loop()

    GPIO.cleanup()

    sys.exit(exit_code)

if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser()

    parser.add_argument('datadir', type=str)
    parser.add_argument('logdir', type=str)
    parser.add_argument('linuxiodir', type=str)
    parser.add_argument('--period', type=float, default=1.0, help='Delay between iterations.')
    parser.add_argument('--file-rotation-period', type=int, default=86400, help='Create a new log and data file after this time in seconds')
    parser.add_argument('--file-deletion-period', type=int, default=1000000, help='Delete old files after this period in seconds')

    parser.add_argument('--DAY-START-HHMM', type=int, default=800, help='e.g. 800 for 8AM, 2000 for 8PM')
    parser.add_argument('--NIGHT-START-HHMM', type=int, default=1900, help='e.g. 1900 for 7PM')
    
    #parser.add_argument('--heat_lamp_relays', type=int, nargs='+', default=[2], help)
    parser.add_argument('--TEMP-DAYTIME', type=int, default=32, help='Temperature (Celcius) target at ~3PM (coldest part of the night)')
    parser.add_argument('--TEMP-NIGHTTIME', type=int, default=27, help='Temperature (Celcius) target at ~3AM (hottest part of the day)')
    parser.add_argument('--TEMP-CONTROL-BOUNDS', type=int, default=1, help='Temperature swing allowed away from nominal for bang-bang control.')
    parser.add_argument('--TEMP-LOW-CRITICAL', type=int, default=25, help='Lower Alarm Temperature in Celcius')
    parser.add_argument('--TEMP-HIGH-CRITICAL', type=int, default=40, help='Upper Alarm Temperature in Celcius')
    
    args = parser.parse_args()
    
    main(args)