#Example code for taking a measurement and printing it out

import traceback #verbose error output
from time import time, sleep #time functions
import Sensor.libC12880MA as libC12880MA #C12880MA control library
from Sensor.models import SpectralReading
hama = libC12880MA.pi4_hama() #init the library
from gpiozero import PWMLED #PWM control library
led = PWMLED(26) #set LED pin for PWM control

#gather wavelength data and init one measurement
try:
    print('Starting..')
    nm = hama.wavelength()
    hama.measure(1e-6, 1) #do one initial measurement because first spectra after power-up will be noise
except:
    traceback.print_exc()

exposition = 1e-2 #set exposition time
accum = 1 #set amount of spectral accumulations
pwmvalue = 0.2 #set duty % in the range [0:1]

#continiously perform measurements at the parameters specified above
while True:
    try:
        led.value = pwmvalue #turn LED on
        startTime = time() #measurement starting time
        spectra = hama.measure(exposition, accum) #exposition in seconds, accumulations as int
        endTime = time() #measurement finishing time
        led.value = 0 #turn LED off

        reading = SpectralReading(
            channels=spectra,
            wavelengths=nm.tolist(),
            exposition=exposition,
            accumulations=accum,
        )

        min_val, min_nm = reading.min_value()
        max_val, max_nm = reading.max_value()

        #print output
        print('\nSpectra:', reading.channels)
        print(f'Max: {max_val}@{round(max_nm)}[nm]; Min: {min_val}@{round(min_nm)}[nm]')
        print('Runtime: %.6f[s];'%(endTime-startTime),
              'Exposition: %.2E[s];'%exposition,
              'Accumulations: %d'%accum)
        #for long runtimes blame time.sleep() and system scheduling

        sleep(1e-3) #delay until next measurement starts

    except KeyboardInterrupt:
        break
    except:
        traceback.print_exc()
        break
#finalize program
print('\nEnding..')
hama.cleanup()

