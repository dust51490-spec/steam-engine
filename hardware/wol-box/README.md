# Wake-on-LAN box

A small ESPHome device that switches on a PC that is connected over Wi-Fi. Wake-on-LAN over Wi-Fi
usually works only from sleep, not from off. The box joins your home network over Wi-Fi and sends
the magic packet through a short network cable that goes straight into the PC's LAN port. The PC
keeps using Wi-Fi for everything else.

```
Home Assistant ──Wi-Fi──> WoL box (WT32-ETH01) ──LAN cable──> LAN port of the PC
```

If your PC is connected by cable to your router anyway, you do not need this box: use the button of
the *Wake on LAN* integration instead.

## Parts

- WT32-ETH01 (ESP32 with LAN8720 Ethernet)
- a short network cable
- a USB cable with 5 V and GND on jumper wires (or a cut USB cable) for power
- for the first flash only: a USB-serial adapter with 3.3 V logic and a few jumper wires

No case is needed. A piece of heat-shrink tube keeps the board from touching metal.

## Power

The box gets its power from a USB port of the PC: USB 5 V to the `5V` pin, GND to `GND`. That port
has to deliver power while the PC is off. In the BIOS this usually means *ErP* (or *EuP*,
*Deep Sleep*) off; some boards also have an option like *USB power in S5*. Check it by shutting the
PC down: the red LED of the WT32-ETH01 has to stay on. If no port keeps its power, use any 5 V phone
charger instead.

## First flash

1. Copy [`wol-box.yaml`](wol-box.yaml) into the ESPHome Device Builder and change the
   `substitutions` at the top: the MAC address of the PC's LAN port (Windows: `Get-NetAdapter`) and a
   free, fixed address of the box in your home network (reserve it in the router).
2. Add to your ESPHome `secrets.yaml`: `wifi_ssid`, `wifi_password`, `wol_box_api_key` (a new key,
   e.g. `openssl rand -base64 32`) and `wol_box_ap_password` (password of the fallback hotspot).
3. Connect the USB-serial adapter: adapter TX to `RX0`, adapter RX to `TX0`, GND to `GND`, 5 V to
   `5V` (or 3.3 V to `3V3`). Do not connect the PC's USB power at the same time.
4. Connect `IO0` to `GND` and then power the board up: it starts in flash mode.
5. In the Device Builder choose *Install* → *Manual download*, and flash the file with
   [web.esphome.io](https://web.esphome.io) (or `esphome run wol-box.yaml --device COM3`).
6. Remove the `IO0` wire and the adapter. All later updates go over the air.

## Connect it to the PC

1. Plug the network cable into the box and into the LAN port of the PC whose MAC address you used.
2. BIOS: turn on Wake-on-LAN (often called *Power On By PCI-E* or *Wake on LAN*), *ErP* off.
3. Windows, Device Manager → the network adapter:
   - *Power Management*: allow this device to wake the computer, only with a magic packet.
   - *Advanced*: *Wake on Magic Packet* on. Turning *Energy Efficient Ethernet* off can help when
     the link goes to sleep.
4. The LAN adapter gets a self-assigned address (169.254.x.x) and shows "no internet". That is
   expected; Windows keeps using Wi-Fi for the network. It worked here with Fast Startup on; if the
   PC does not start from off, try turning Fast Startup off.

## Home Assistant

1. Add the box under *Settings* → *Devices & services* (ESPHome is usually found by itself) and enter
   the API key from your `secrets.yaml`.
2. On the Steam Engine settings page choose its button (`Power on`) in *Turn on with*.
3. Optional: a Ping sensor for the PC as *PC reachable*.
4. Test: shut the PC down, press the button. The PC should start within a few seconds.
