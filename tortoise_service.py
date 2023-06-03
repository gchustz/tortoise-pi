import RPi.GPIO as GPIO
import glob
import time
import datetime
import pytz
import sys
import os

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
    UV_GPIO_CHANNEL = RELAY_CHANNELS[1]
    HEAT_LAMP_GPIO_CHANNELS = [
        RELAY_CHANNELS[2],
        RELAY_CHANNELS[3]
    ]

    def __init__(self, args): 

        self.period = args.period
        self.curr_datetime = ''
        self.curr_time = time.time()
        self.HHMM_time = 0
        self.curr_TEMP = 0.
        self.heartbeat = 0

        # UV
        self.UV_CMD_STATE = RELAY_OPEN
        self.UV_GPIO_STATE = None
        self.UV_TURN_ON_TIME = args.UV_ON_HHMM
        self.UV_TURN_OFF_TIME = args.UV_OFF_HHMM
        # TODO: change to day/night 

        # heat
        self.HEAT_CMD_STATE = RELAY_OPEN
        self.HEAT_GPIO_STATE = None 
        self.HEAT_TURN_ON_TEMP = args.TEMP_HEAT_ON
        self.HEAT_TURN_OFF_TEMP = args.TEMP_HEAT_OFF 
        # TODO: add night temperature reduction
        
        self.HEAT_CRIT_LOW = args.TEMP_LOW_CRITICAL
        self.HEAT_CRIT_HIGH = args.TEMP_HIGH_CRITICAL
        assert self.HEAT_CRIT_LOW < self.HEAT_TURN_ON_TEMP < self.HEAT_TURN_OFF_TEMP < self.HEAT_CRIT_HIGH
        
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
        if self.HEAT_CMD_STATE == RELAY_OPEN and (self.curr_temp <= self.HEAT_TURN_ON_TEMP):
            self.HEAT_CMD_STATE = RELAY_CLOSED

        elif self.HEAT_CMD_STATE == RELAY_CLOSED and (self.HEAT_TURN_OFF_TEMP <= self.curr_temp):
            self.HEAT_CMD_STATE = RELAY_OPEN

        if self.HEAT_GPIO_STATE is None or self.HEAT_CMD_STATE != self.HEAT_GPIO_STATE:
            for heat_channel in self.HEAT_LAMP_GPIO_CHANNELS:
                GPIO.output(heat_channel, self.HEAT_CMD_STATE)

            self.HEAT_GPIO_STATE = self.HEAT_CMD_STATE

        # TODO: add timeout for heat lamp
        
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

        temp_state_str = f'| TEMP {self.HEAT_CRIT_LOW}/{self.HEAT_TURN_ON_TEMP}/{self.curr_temp}/{self.HEAT_TURN_OFF_TEMP}/{self.HEAT_CRIT_HIGH} C -> [{self.HEAT_GPIO_STATE}/{self.HEAT_CMD_STATE}] '
        log_str += temp_state_str

        uv_state_str = f'| UV {self.UV_TURN_ON_TIME}/{self.HHMM_time}/{self.UV_TURN_OFF_TIME} -> [{self.UV_GPIO_STATE}/{self.UV_CMD_STATE}] '
        log_str += uv_state_str

        for line in lines:
            log_str += '\nERROR: ' + str(line)

        log_str += '\n'

        with open(self.logpath, 'a') as logf:
            logf.write(log_str)

    def loop(self):
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

    parser.add_argument('--UV-ON-HHMM', type=int, default=800, help='e.g. 800 for 8AM, 2000 for 8PM')
    parser.add_argument('--UV-OFF-HHMM', type=int, default=1900, help='e.g. 1900 for 7PM')
    
    #parser.add_argument('--heat_lamp_relays', type=int, nargs='+', default=[2], help)
    parser.add_argument('--TEMP-HEAT-ON', type=int, default=30, help='Lowerbound Temp in Celcius to turn on heatlamp')
    parser.add_argument('--TEMP-HEAT-OFF', type=int, default=33, help='Upperbound Temp in Celcius to turn off the heatlamp')
    parser.add_argument('--TEMP-LOW-CRITICAL', type=int, default=27, help='Lower Alarm Temperature in Celcius')
    parser.add_argument('--TEMP-HIGH-CRITICAL', type=int, default=40, help='Upper Alarm Temperature in Celcius')
    
    args = parser.parse_args()
    
    main(args)