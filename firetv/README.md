# Xos TV (Fire TV Stick)

A small Fire TV / Android TV app that shows one Xos screen full-screen: the Lamar room TV by
default. It also works for the door panel, lobby signage, and the dashboard wall. It is a
locked-down WebView over the same `app/` kiosk views the Raspberry Pi node runs. No Gradle and no
Google Play services, so it sideloads onto any Fire OS stick.

```
firetv/
├── AndroidManifest.xml      # leanback launcher entry, cleartext allowed for a LAN hub
├── src/com/example/xos/tv/  # MainActivity (WebView + remote menu), Config, BootReceiver
├── res/drawable/            # icon + 320×180 TV banner
└── build.sh                 # aapt2 → javac → d8 → zipalign → apksigner
```

## Build

```bash
brew install --cask android-commandlinetools android-platform-tools
brew install openjdk
sdkmanager --sdk_root="$HOME/Library/Android/sdk" "build-tools;35.0.0" "platforms;android-34"
./firetv/build.sh            # → firetv/build/xos-tv.apk
cp firetv/build/xos-tv.apk firetv/xos-tv.apk   # publish: the static site serves it on merge
```

Bump `versionCode`/`versionName` in `AndroidManifest.xml` and `BuildInfo.VERSION` for each
published build. Builds are signed with the local debug key, so a stick upgrading from a build made
on another machine needs `adb uninstall com.example.xos.tv` first.

## Where the screen loads from

| Source | URL | Live sync |
|---|---|---|
| **Xos server** (default) | `https://xos.example.org/app/?kiosk=1&device=…&client=firetv-…#/tv/lamar` | Yes: with every controller, panel and screen on the server |
| Pi hub on the LAN | `http://<pi>:7000/app/?kiosk=1…#/tv/lamar` | Yes: devices on that hub only |

The stick appears in **Admin → Devices** on the server, listed by its device id (`firetv-xxxxxxxx`).

## Set up from Admin by IP address (no laptop)

Turn on ADB debugging on the stick (below), then in Admin → Screens → **Add a screen** enter its
IP address, a name and what to show, and press **Set up**. The Xos server runs the same steps as
`provision.sh` over network ADB (`src/app/provision.py`) and names the screen when it checks in.
Accept **Allow USB debugging?** on the TV the first time and tick **Always allow**. This works
for Android TV boxes and Android smart boards too.

## Set up a Fire TV Stick (one command)

**On the stick:**

1. Go to Settings → My Fire TV → About and click the device name 7 times.
2. Go to Developer Options and turn on **ADB debugging**.

**Then, from a laptop on the same network:**

```bash
firetv/provision.sh 192.0.2.120                 # shows a pairing code → Admin → Devices → Add a screen
firetv/provision.sh 192.0.2.168 signage/hq  # any kiosk route
firetv/provision.sh 192.0.2.120 tv/lamar https://xos.example.org --reboot-test
```

Accept **"Allow USB debugging?"** on the TV. The script then:

- installs Xos TV;
- grants it auto-start permission ("display over other apps", required on Android 10+ / Fire OS 8);
- points it at the screen and opens it;
- checks it's on screen and saves a screenshot to `firetv/build/`.

`--reboot-test` also reboots the stick and checks that Xos opens by itself.

**Without a laptop:** install *Downloader* from the Amazon Appstore and enter
`https://xos.example.org/firetv/xos-tv.apk`. Then open **Xos TV** once. On Fire OS 7
(Android 9) it then starts by itself. On Fire OS 8 it needs the auto-start permission
(Menu → *Auto-start permission*, or the script above). Without it, open Xos TV once after a
power cut.

## Remote control from the Xos app

Open **Admin → Devices** in <https://xos.example.org/app/> and select **Control** on a stick.
Commands reach the stick in about a second over its own connection to the server, so they work
through office NAT.

| Control | How it works on the stick |
|---|---|
| Switch screen | Saves the new screen, so it survives reboots, and switches in place with a fade through black. Builds before 0.6.0 reload to switch. |
| Screenshot | The app captures its WebView and uploads a JPEG. It appears in the panel. |
| Identify / message | Full-screen banner on the TV for 10–15 s. |
| Reload | Clears the cache and reloads, picking up a new deploy. |
| Display off / on | Puts the stick to sleep or wakes it. With HDMI-CEC on, the TV turns off, or on and to this input. Needs the device admin that `provision.sh` enables. While asleep the stick stays on Xos (below), so Display on reaches it. |
| Switch TV to Xos | Display off, then Display on, run by the server (below). With HDMI-CEC on, the TV switches back to the stick's input. Xos TV 0.6.0 or later with display control. |
| Return to Xos | Minutes before the stick comes back after someone presses Home. |

The page calls the app through `window.XosNative` (`NativeBridge.java`). Only pages from the
configured Xos server can use it.

## Switch TV to Xos

Someone in the room switches the TV to another HDMI input, and it stays there. **Switch TV to
Xos** takes it back. It's in Admin → Screens → Control and on the room remote (next to TV On /
Off, and under More). Screen operators, Admins and room remotes can use it, like Display on/off.

It needs no new APK. The server runs commands every 0.6.0 stick already has
(`switch_input` in `src/app/main.py`):

1. **Display off.** The stick sleeps, and SleepWatch keeps it connected.
2. It waits until the stick checks in asleep, then 5 s more. Many TVs ignore HDMI-CEC while they
   go to standby.
3. **Display on.** SleepWatch wakes the stick. HDMI-CEC one-touch play turns the TV on and
   switches it to the stick's input.

A stick that's already asleep only gets step 3. It takes about 10 s, and the command history
shows each step and the result. Only one run per stick at a time; a second gets `409`, and so
does Display on or off for that stick until the run finishes.

If a step times out before it reaches the stick, it expires, so a late check-in can't turn the
TV off or on after Xos said the switch failed. If Display off reached the stick but wasn't
confirmed, Xos still sends Display on, so the TV isn't left off. Forgetting the screen in Admin
stops the run.

The button is disabled, with the reason, and the server answers `409` for:

- Xos TV before 0.6.0: a sleeping stick drops off Xos, so step 3 would never reach it;
- a stick without display control (run `provision.sh`);
- a screen that isn't the Xos TV app, or a stick that isn't connected right now.

HDMI-CEC must be on in the TV's settings (it has a brand name: Anynet+, Bravia Sync, SimpLink…),
and the TV must allow one-touch play. Xos never switches the input by itself: only a person, a
room remote or an MCP agent (`switch_tv_to_xos`) starts it.

## Staying on screen

| Situation | What Xos TV does |
|---|---|
| Stick boots or the power comes back | Opens Xos TV, which loads the live room screen. |
| Someone presses Home or opens another app | Comes back after 2 min. Change this under Menu → *Return to Xos after leaving*; 0 is off. |
| Xos TV is updated | Reopens straight away. |
| The stick sleeps (Display off, or the TV switched off with its own remote over HDMI-CEC) | Keeps its connection to Xos (`SleepWatch.java`, a foreground service holding the CPU and Wi-Fi awake). Xos shows it as "TV off", and Display on wakes it. It doesn't come back on by itself. |
| Network or server down | Shows an offline message and retries every 15 s. |
| The page crashes or is killed for memory | Reloads itself. |
| Menu left open | Closes after 60 s. |

Fire OS won't let an app replace the Amazon home screen, so Home still opens it. The return
timer is what brings the room screen back.

Validated on an Android 14 TV emulator (SDK 34), the strictest case: provisioning, a cold
reboot, Home followed by the return, and an app update. Not yet run on a physical Fire Stick.

### Before 0.6.0

On Xos TV 0.5.0 and older, a sleeping stick dropped off Xos: the page stops, so nothing heard
Display on, and the TV stayed off until someone pressed the stick's remote. The page now refuses
Display off on those builds. Update a stick by running Admin → Add a screen → by IP again (or
`provision.sh`); it keeps its id, name and screen. For a stick that's already asleep, Display on
also tries to wake it over network ADB (`wake_adb` in `src/app/provision.py`). That only works
when the Xos server can reach the stick and set it up before, and the stick's network stays up
while it sleeps. Otherwise press a button on its remote.

Publish builds from the machine that signed the earlier ones (the same debug key). A stick
won't take an update signed with a different key, and uninstalling it would give it a new id.

## Using it with the remote

- **Menu (≡) or Back** opens settings: pick the screen, switch between the cloud and a hub,
  enter a custom URL, toggle start-on-boot, reload, or exit.
- If the page can't load, Xos TV shows the URL and retries every 15 s.
- The screen stays on while Xos TV is open.

## Limits

- Fire TV Sticks on **Vega OS** (2025+ Fire TV Stick 4K Select) don't run Android APKs. Use a
  Fire OS stick, or a Pi node.
- If your Xos server only resolves on the office network, a stick on guest Wi-Fi can't load it.
- There's no HDMI-CEC TV power control from the app. Fire OS handles CEC itself.
- The stick shows what Admin assigns it; meeting control (Zoom) runs on the server, from the
  room's table remote.
