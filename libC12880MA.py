from time import sleep, time #time related functions
import RPi.GPIO as GPIO #Raspberry Pi GPIO interfacing library
import numpy as np

import busio
import board
import adafruit_ads1x15.ads1115 as ads1115
from adafruit_ads1x15.analog_in import AnalogIn
from adafruit_ads1x15.ads1x15 import Mode

class pi4_hama:
    #Initialize parameters
    def __init__(self):
        GPIO.setwarnings(False)			#Removes some clutter from shell
        GPIO.setmode(GPIO.BCM)			#GPIO numbering mode
        self.CLK = 18					#C12880MA CLK pin GPIO nr
        self.ST = 17					#C12880MA ST pin GPIO nr
        GPIO.setup(self.CLK, GPIO.OUT)	#Set CLK pin as output
        GPIO.setup(self.ST, GPIO.OUT)	#Set ST pin as output
        GPIO.output(self.CLK, 1)		#init CLK HIGH
        GPIO.output(self.ST, 0)			#init ST LOW

        self.ADC_CHAN = 0		#Corresponding ADS1115 channel
        self.adc_delay = 2e-3	#Delay between ADC reads, ADS1115 max SPS is 860, so a rounded up 2ms delay is used
        self.exposition = 1e-4	#init exposition time
        self.delay = 1e-6		#delay between CLK pulse shifts
        self.accum = 1			#init number of spectra accumulations

        self.i2c = busio.I2C(board.SCL, board.SDA)
        self.ads = ads1115.ADS1115(self.i2c)
        self.ads.mode = Mode.SINGLE
        self.ads.data_rate = 860
        self.chan = AnalogIn(self.ads, 0)   # canal A0

    #C12880MA wavelength calibration (device specific, each device gets shipped with this information)
    def wavelength(self):
        i = np.arange(288)
        self.nm = (3.11487118e2 + 2.704770136*i - 1.198161774e-3*i**2
                   - 7.333264294e-6*i**3 + 8.195191046e-9*i**4
                   + 6.104509768e-12*i**5)
        return self.nm

    #Repeat function *f* certain amount of *times*
    def repeater(self, times, f, *args):
        for _ in range(times):
            f(*args)

    #Exposition
    def exposition_control(self):
        starting_time = time()
        to_reach_exposition = starting_time + self.exposition
        while to_reach_exposition > time():
            GPIO.output(self.CLK, 1)
            sleep(self.delay)
            GPIO.output(self.CLK, 0)
            sleep(self.delay)

    #One CLK cycle
    def cycle_spectrometer(self):
        GPIO.output(self.CLK, 1)
        sleep(self.delay)
        GPIO.output(self.CLK, 0)
        sleep(self.delay)

    #One read cycle (appends to the provided list)
    def _read_pixel(self, spectra):
        GPIO.output(self.CLK, 1)
        spectra.append(self.chan.value)
        GPIO.output(self.CLK, 0)

    #Spectrometer control
    def start(self):
        spectra = []
        try:
            #initialization
            GPIO.output(self.CLK, 0)
            self.cycle_spectrometer()
            GPIO.output(self.ST, 1)
            sleep(self.delay)
            self.repeater(3, self.cycle_spectrometer)

            #---Integration time start
            self.exposition_control()
            GPIO.output(self.ST, 0)
            self.repeater(48, self.cycle_spectrometer)
            #---Integration time end

            #Cycle 48 times before reading resulting spectra
            self.repeater(39, self.cycle_spectrometer)

            #Read spectrometer
            for _ in range(288):
                self._read_pixel(spectra)

            #Finishing pulses
            self.repeater(8, self.cycle_spectrometer)
            GPIO.output(self.CLK, 1)
            sleep(self.delay)
        finally:
            GPIO.output(self.CLK, 1)

        return spectra

    #Single acquisition (no accumulation)
    def acquisition(self):
        return self.start()

    def cleanup(self):
        GPIO.cleanup()

    #Main
    def measure(self, exposition, accum):
        self.exposition = exposition
        self.accum = accum
        if type(accum) == float:
            self.accum = round(self.accum)

        spectra = np.array(self.acquisition(), dtype=np.float64)
        if self.accum > 1:
            for _ in range(self.accum - 1):
                spectra += np.array(self.acquisition(), dtype=np.float64)
            return spectra.tolist()
        else:
            return spectra.tolist()
