import rp2                            # type: ignore
from machine import Pin, Timer        # type: ignore

from uctypes import addressof         # type: ignore

import dma

# Interface to a DMX universe for sending using a PIO module.

# Quick theory of operation:

# DMX basics:
#     DMX data frames comprise a long (90+us) "break" as a logic low, followed by a 16+us "MarkAfterBreak" as a logic high, 
#     then a series of bytes in 8N2 MSB-first format at 4us/bit. Each data frame is known as a Universe and comprises a 
#     single-byte Start Code (0 for DMX) followed by between 1 and 512 data bytes, one byte per lighting channel

# Class basics:
#     Put simply, the class sets up the DMA and PIO and then provides a convenient interface to the bytearray used by the
#     DMA controller. Setting or reading individual channels is permitted, as is reading/writing the entire Universe. 
# 
#     When used as a transmitter, the class uses DMA and PIO to repeatedly send the universe based upon a timer. 
#     When used as a receiver, each received universe is copied and made available to the user as soon as it is received.

# DMA Channel and PIO allocations:
#     It is not possible to check the hardware to see if a DMA channel or PIO statemachine is already in use. No extra locking
#     has been added in this software, thus clashes need to be avoided by the user code.

# Links to original references:
# DMX in C++ with PIO and DMA: https://github.com/jostlowe/Pico-DMX
#
# MicroPython SDK: https://datasheets.raspberrypi.com/pico/raspberry-pi-pico-python-sdk.pdf
# RP2 MicroPython documentation: https://docs.micropython.org/en/latest/library/rp2.html
# RP2040 datasheet: https://datasheets.raspberrypi.com/rp2040/rp2040-datasheet.pdf
#
# PIO and DMA: https://pythonrepo.com/repo/benevpi-RP2040_micropython_dma-python-programming-with-hardware or https://github.com/benevpi/RP2040_micropython_dma?ref=pythonrepo.com
# DMA discussions on the MicroPython forum and other websites:
#   * https://forum.micropython.org/viewtopic.php?f=21&t=10717
#   * https://forum.micropython.org/viewtopic.php?f=21&t=9697
#   * https://www.instructables.com/Arbitrary-Wave-Generator-With-the-Raspberry-Pi-Pic/
#
# DMX timing: https://support.etcconnect.com/ETC/FAQ/DMX_Speed

class DMX_TX:
    """ Interface to a DMX universe for sending using a PIO module.
    Transmission:
        A DMA channel is set up to copy a bytearray (address automatically incrementing on each transfer) into the PIO FIFO 
        (at a fixed address). When this transfer completes, the DMA channel raises an interrupt and the handler resets both the 
        PIO and the DMA channel. When restarted, the PIO first ensures that the DMX "break" is sent, then the 
        MarkAfterBreak, before streaming out the bytes as sent by the DMA into its FIFO. As the PIO completes each byte, a 
        Data Request (DREQ) interrupt is raised to start the next DMA transfer. The need to send the Break and MAB by resetting
        the PIO are the reason we can't just chain two DMA channels together where the second channel simply reloads the first.

        1. PIO sends Break and half of the MAB - the second half of the MAB comes from the stop bits which are sent next
        2. PIO then pulls data from the DMA, triggering a DREQ, and sends the stop bits (8us), the start bit (4us), then 8 data bits. 
        2. Upon receipt of the DREQ, DMA sends the next byte to the PIO input FIFO
        3. When the entire Universe has been DMA'd, the DMA raises a processor interrupt
        4. When the DMA interrupt is received, the processor resets the PIO and restarts the DMA 
    """
    from dmx_asm import dmx_out

    def __init__(self, pin, universe_size=512, statemachine=0, dmachannel=0):
        """ Initialisation of the DMX controller PIO statemachine and DMA channel

        Args:
            pin (numeric):                  Pin number to use
            universe_size (int, optional):  Size of the DMX universe to interface to. Defaults to 512.
            statemachine (int, optional):   Which PIO statemachine should be used. Defaults to 0.
            dmachannel (int, optional):     Which DMA channel should be used. Defaults to 0.

        Raises:
            ValueError: Any invalid parameters are reported as exceptions

        TODO: Allow the interbyte delay to be specified
        TODO: Allow the inter-packet delay to be specified (or is the rate good enough?)
        """
        if universe_size < 1 or universe_size > 512:
            raise ValueError("DMX universes must have 1...512 channels")
        
        self.channels       = bytearray([0 for _ in range(universe_size+1)]) # +1 because DMX-0 is the start code, with channels 1-512 behind it

        self._pin           = Pin(pin, Pin.OUT, Pin.PULL_UP)
        self._sm            = rp2.StateMachine(statemachine, 
                                               prog=DMX_TX.dmx_out, 
                                               freq=1_000_000, 
                                               sideset_base=self._pin, 
                                               out_base=self._pin)
        self._dma           = dma.DmaChannel(dmachannel)

        # Set up the DMA controller
        self._dma.NoWriteIncr()
        self._dma.SetTREQ(0) # TODO - hard coded as PIO0 TX0

    def start(self, period = 50):
        """ Start sending DMX packets

        Args:
            period (int, optional): Start sending a new packet every period milliseconds. Defaults to 50.
        """
        self.timer_count = 0
        self.t = Timer(period=period, callback=self.restart)

    def pause(self):
        self.t.deinit()
        self._sm.active(0)

    def restart(self, t):
        self._sm.active(1)
        self._sm.restart()
        self._dma.SetChannelData(addressof(self.channels), 0x50200010, len(self.channels), True) # TODO Hard coded as PIO0 for now
        self.timer_count += 1
   
    def __del__(self):
        # TODO - tidy up the state machine and DMA channels
        pass
        
    #def __repr__(self):
        # TODO - encode the class state - including which PIO and DMA channel are in use
        pass

    def __str__(self):
        result = ""
        for chan in range(len(self.channels)):
            if chan % 20 == 1:
                result += f"\n{chan:03}:"                      # Start a new line with the channel number every 20 lines
            
            if chan % 5 == 1:
                result += "  "                                 # Put spaces into the line every five channels
            
            if chan == 0:
                result = f"Start code: {self.channels[chan]}"  # The start code ("channel zero") is formatted differently
            else:
                result += f" {self.channels[chan]:3}"
            
            if chan % 100 == 0:                                # Blank line every 100 channels
                result += "\n"
        
        result += "\n"
        return result
class DMX_RX:
    """
    Class basics:
        Set up one channel of DMA and one PIO to receive DMX frames of the specified (or shorter) length, and provide a convenient
        interface to a bytearray containing the values received. Reading individual channels is permitted, as is reading the entire 
        Universe. 

    Caution:    
        There is a small race condition in that IF a short frame is received AND there is a relatively short BREAK, the processor 
        may not have handled the PIO interrupt (6b) fully before the BREAK has finished causing data corruption.

    How it works:
        A PIO is constantly watching the DMX input pin. Once a valid Break and MarkAfterBreak are observed, subsequent 8N2 
        bytes are passed to the PIO FIFO which is read by the DMA channel and copied into the bytearray.

        1. The PIO waits for a very long run of zeros (92us or more - Break)
        2. The PIO waits for a one (any length - MAB) - there is no check that this is longer than then minimum length (12us)
        3. The PIO waits for a zero (start bit) and starts to sample the data ~6us later - the start bit should be 4us long
        4. The PIO captures 8 bits, one every 4us, and shifts these into the ISR
        5. The PIO waits for a one (stop bit), and sends the ISR to the DMA
        6a. If a full frame has NOT yet been received, the PIO loops back to step 3 - there is no check that the stop bit is the correct length (8us)
        6b. If a full frame HAS been received, the PIO causes a processor interrupt, the handler for which resets the DMA. The PIO is already back at step 1.
         
        The DMA accepts each byte from the PIO and stores it in memory. If too many bytes are received (should be impossible), the DMA stops

    DMA Channel and PIO allocations:
        It is not possible to check the hardware to see if a DMA channel or PIO statemachine is already in use. No extra locking
        has been added in this software, thus clashes need to be avoided by the user code.
    """

    from dmx_asm import dmx_in

    def __init__(self, pin, statemachine=4, dmachannel=1, num_channels=512):
        """ Initialisation of the DMX controller

        Args:
            pin (numeric):                  Pin number to use
            statemachine (int, optional):   Which PIO statemachine should be used. Defaults to 4.
            dmachannel (int, optional):     Which DMA channel should be used. Defaults to 1.
            num_channels (int, optional):   The number of DMX channels expected. Defaults to 512. 
            
            Note that if shorter frames are being received AND the DMX BREAK is close to the minimum permitted, a race condition exists which may cause
            data corruption.

        Raises:
            ValueError: Any invalid parameters are reported as exceptions
        """
        self.channels   = bytearray([0 for _ in range(num_channels+1)]) # DMX-0 is the start code, with channels 1-512 behind it
        
        self._pin       = Pin(pin, Pin.IN)

        self._debugpin  = Pin(12, Pin.OUT, Pin.PULL_UP) # TODO Temporary hard coded debug pins (12 + 13 used)

        self._sm = rp2.StateMachine(statemachine, 
                                    prog=DMX_RX.dmx_in, 
                                    freq=1_000_000,
                                    in_base=self._pin, 
                                    jmp_pin=self._pin,
                                    sideset_base=self._debugpin)
        self._sm.irq(handler=self.IRQ_from_PIO)
        self.frames_received = 0
        
        self._dma = dma.DmaChannel(dmachannel)
        self._dma.NoReadIncr()
        self._dma.SetTREQ(dma.TREQ_PIO1_RX) # TODO hard coded as PIO1 RX for now


    def start(self):
        self._dma.SetChannelData(0x50200027, addressof(self.channels), len(self.channels), True) # TODO Hard coded as PIO1 RX +3 (LSB byte without shifting)
        self._sm.restart()
        self._sm.put(len(self.channels)-1)    # Set the length of the DMX frame we expect
        self._sm.active(1)
    
    def pause(self):
        self._sm.active(0)

    def __del__(self):
        # TODO - tidy up the state machine and DMA channels
        pass
        
    def __repr__(self):
        # TODO - encode the class state - including which PIO and DMA channel are in use
        pass

    def __str__(self):
        result = ""

        for chan in range(len(self.channels)):
            if chan % 20 == 1:
                result += f"\n{chan:03}:"                      # Start a new line with the channel number every 20 lines
            
            if chan % 5 == 1:
                result += "  "                                 # Put spaces into the line every five channels
            
            if chan == 0:
                result = f"Start code: {self.channels[chan]}"  # The start code ("channel zero") is formatted differently
            else:
                result += f"{self.channels[chan]:3}"
            
            if chan % 100 == 0:                                # Blank line every 100 channels
                result += "\n"
        
        result += "\n"
        return result
    
    def IRQ_from_PIO(self, sm):
        # When a byte of data is received, use DMA copy into a memory mapped channel variable
        self._dma.SetChannelData(0x50200027, addressof(self.channels), len(self.channels), True) # TODO Hard coded as PIO1 RX
        self.frames_received += 1


def test_rx():
    """Test DMX data can be received by the pico"""
    # Initialise the DMX receiver
    dmx_start = 0

    dmx_in = DMX_RX(pin=28, statemachine=1)  # DMX data should be presented to GPIO28 (Pico pin 34)
    dmx_in.start()
    last_frame = -1

    while True:
        brightness = dmx_in.channels[dmx_start + 0]
        red = dmx_in.channels[dmx_start + 1]
        green = dmx_in.channels[dmx_start + 2]
        blue = dmx_in.channels[dmx_start + 3]
        fade = dmx_in.channels[dmx_start + 4]
        speed = dmx_in.channels[dmx_start + 5]

        current_frame = dmx_in.frames_received

        if (current_frame != last_frame):
            last_frame = current_frame
            print(f"Received fader:{brightness}  R:{red} G:{green} B:{blue} B:{fade} B:{speed}")

