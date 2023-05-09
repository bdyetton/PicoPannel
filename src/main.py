import dmx
import utime
import machine
import neopixel


def signal_online():
    """"""
    led = machine.Pin("LED", machine.Pin.OUT)
    led(1)
    utime.sleep(0.5)
    led(0)
    utime.sleep(0.5)
    led(1)
    utime.sleep(0.5)
    led(0)
    utime.sleep(0.5)
    print('PicoPannel Online')


if __name__ == "__main__":
    signal_online()
    neopixel.test()
    dmx.test_rx()
