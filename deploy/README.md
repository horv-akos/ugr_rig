# Setting up the Pi

Written at the desk and **not yet tried on the Pi**: treat every step as a checklist to confirm,
and fix this file where reality differs. Raspberry Pi 4 only (pigpio has no Pi 5 support).
Wiring and power-up order: `docs/CONNECTIONS.md`.

## 1. OS

Raspberry Pi OS, 64-bit: Lite, or the desktop image when a screen at the site is wanted.
Check with `dpkg --print-architecture` (must say `arm64`).

Create the user `ugr` (or change the paths in `ugr-rig.service` and the user in
`ugr-shutdown.sudoers`), enable SSH.

```
sudo usermod -aG dialout,plugdev,gpio ugr   # UART, camera USB, GPIO
```

### Desktop image only

Its GVFS volume monitors (`gvfs-gphoto2-volume-monitor`, `gvfs-mtp-volume-monitor`) claim
the camera's USB interface as soon as it is plugged in, and the rig then gets `CameraBusy`.
Mask them for every user who logs in to the desktop:

```
sudo systemctl --global mask gvfs-gphoto2-volume-monitor.service gvfs-mtp-volume-monitor.service
sudo reboot
pgrep -af 'gvfs.*(gphoto2|mtp)'    # with the camera on: must print nothing
```

If something still shows up, remove the backends instead: `sudo apt remove gvfs-backends`
(read what apt wants to take with it before saying yes). Masking stops the desktop from showing
the camera as a drive; the rig never needs that.

On site the desktop's Chromium opens the pages at `http://localhost:8000/home`. Turn the
monitor off (or away) while a study records: a lit screen in the cab is a light source and
reflects in the glazing. If the desktop is only wanted now and then,
`sudo systemctl set-default multi-user.target` boots to the console, and
`sudo systemctl start graphical.target` starts the desktop when needed.

## 2. UART for the drivers

The TMC2209s are configured over the PL011 UART on GPIO14/15 (`docs/CONNECTIONS.md` §6).

In `/boot/firmware/config.txt`, under `[all]`:

```
enable_uart=1
dtoverlay=disable-bt
```

Then `sudo raspi-config` → Interface Options → Serial Port: login shell over serial **No**,
serial port hardware **Yes**. Reboot. `/dev/serial0` should now point at `ttyAMA0`.

## 3. pigpio

`pigpiod` must run with `-t 1` (PCM clock). Check whether the OS offers it:
`apt policy pigpio`. If not, build it from source (github.com/joan2937/pigpio; `make`,
`sudo make install`, which puts the daemon in `/usr/local/bin`).

```
sudo cp deploy/pigpiod.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now pigpiod
```

## 4. The code

```
cd ~ && git clone <this repository> ugr_rig && cd ugr_rig
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -q tests/
.venv/bin/python -m tools.mountctl --backend sim selftest
```

All wheels exist for 64-bit ARM (numpy, scipy, rawpy, gphoto2 with libgphoto2 bundled). If pip
starts compiling something, stop and check the Python version against the wheels.

## 5. First contact with the hardware, before the service

Power-up order of `docs/CONNECTIONS.md` §7, motor rocker **on**, then:

```
.venv/bin/python -m tools.mountctl drivers      # writes both TMC2209s and reads them back
.venv/bin/python -m tools.ugrctl camera info    # the 2000D over USB
```

`drivers` must end with `verified`. It prints the microstepping (1/16) and the currents the
drivers actually hold. If it says a driver does not answer: motor rail on? serial console off?
1 kOhm on TX? Only after this passes, `mountctl limits`, then the measurements of
`docs/PIVOT_DETAIL.md` §8.

## 6. The service

```
sudo cp deploy/ugr-rig.service /etc/systemd/system/
sudo install -m 0440 deploy/ugr-shutdown.sudoers /etc/sudoers.d/ugr-shutdown
sudo visudo -c                      # must say the files parse; a broken sudoers locks sudo out
sudo systemctl daemon-reload && sudo systemctl enable --now ugr-rig
journalctl -u ugr-rig -f            # prints the URLs it answers on
```

The sudoers file lets user `ugr` run exactly `systemctl poweroff` and nothing else: the
Home page's **Shut down** button. Use it before switching the bench supply off; cutting power
under a running Pi can corrupt the SD card. The button is refused while the head moves, a
study records or analysis runs.

## 7. The Pi's own WiFi

With NetworkManager (the default on current Raspberry Pi OS):

```
sudo nmcli con add type wifi ifname wlan0 con-name ugr-ap autoconnect yes ssid UGR-RIG \
  802-11-wireless.mode ap 802-11-wireless.band bg ipv4.method shared \
  wifi-sec.key-mgmt wpa-psk wifi-sec.psk "<a passphrase of your choice>"
sudo nmcli con up ugr-ap
```

`ipv4.method shared` hands out addresses; the Pi is then `10.42.0.1`, so the pages are at
`http://10.42.0.1:8000/`. Keep an Ethernet cable for SSH while setting this up: once wlan0 is an
access point it no longer joins your normal network.

## Known gaps

- **No clock.** The Pi has no RTC, and on its own WiFi there is no internet time. Study ids and
  timestamps come from the system clock, so after a cold start they can be wrong until the clock
  is set (e.g. `sudo date -s ...` over SSH). Fixing this properly needs a decision: an RTC module
  (hardware), or the page sending the browser's time to the server.
- **Joining another WiFi** (from the desktop's network menu, say, for internet time or updates)
  takes wlan0 off the access point: one radio, one role. `sudo nmcli con up ugr-ap` brings the
  rig's network back.
