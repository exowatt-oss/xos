package com.example.xos.tv;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.net.wifi.WifiManager;
import android.os.Build;
import android.os.IBinder;
import android.os.PowerManager;

import org.json.JSONException;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;

/**
 * Keeps a sleeping stick reachable, so TV on from Xos can wake it.
 *
 * When the stick sleeps (Display off from Xos, or the TV switched off over HDMI-CEC), Fire OS
 * stops the page, and the page is what checks in and hears commands. Without this the stick
 * drops off Xos until someone presses its remote. This foreground service holds the CPU and
 * Wi-Fi awake, keeps the screen's command stream open, and checks in every 30 s saying the
 * display is off. A "display_on" command wakes the stick and opens Xos; MainActivity stops the
 * service once it's back on screen.
 */
public class SleepWatch extends Service {
    private static final String CHANNEL = "sleep";
    private static final int NOTIFICATION_ID = 1;
    private static final long HEARTBEAT_MS = 30_000;
    private static final long RETRY_MS = 5_000;
    private static final int READ_TIMEOUT_MS = 60_000; // the server sends a keepalive every 15 s

    private volatile boolean running;
    private volatile HttpURLConnection stream;
    private Thread worker;
    private PowerManager.WakeLock cpu;
    private WifiManager.WifiLock wifi;

    static void start(Context c) {
        Intent i = new Intent(c, SleepWatch.class);
        try {
            if (Build.VERSION.SDK_INT >= 26) c.startForegroundService(i);
            else c.startService(i);
        } catch (RuntimeException ignored) {
            // Android 12+ refuses from the background; Display off starts it while in front
        }
    }

    static void stop(Context c) {
        c.stopService(new Intent(c, SleepWatch.class));
    }

    @Override
    @SuppressWarnings("deprecation")
    public void onCreate() {
        super.onCreate();
        startForeground(NOTIFICATION_ID, notification());
        PowerManager pm = (PowerManager) getSystemService(Context.POWER_SERVICE);
        cpu = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "xos:sleep-watch");
        cpu.acquire();
        WifiManager wm = (WifiManager) getApplicationContext().getSystemService(Context.WIFI_SERVICE);
        if (wm != null) {
            wifi = wm.createWifiLock(WifiManager.WIFI_MODE_FULL_HIGH_PERF, "xos:sleep-watch");
            wifi.acquire();
        }
        running = true;
        worker = new Thread(this::run, "xos-sleep-watch");
        worker.start();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        running = false;
        HttpURLConnection c = stream;
        if (c != null) c.disconnect();
        if (worker != null) worker.interrupt();
        if (wifi != null && wifi.isHeld()) wifi.release();
        if (cpu != null && cpu.isHeld()) cpu.release();
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    @SuppressWarnings("deprecation")
    private Notification notification() {
        Notification.Builder b;
        if (Build.VERSION.SDK_INT >= 26) {
            NotificationManager nm = (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
            nm.createNotificationChannel(new NotificationChannel(CHANNEL, "TV off", NotificationManager.IMPORTANCE_MIN));
            b = new Notification.Builder(this, CHANNEL);
        } else {
            b = new Notification.Builder(this).setPriority(Notification.PRIORITY_MIN);
        }
        return b.setSmallIcon(getApplicationInfo().icon)
            .setContentTitle("Xos TV")
            .setContentText("TV off · Xos can turn it back on")
            .setOngoing(true)
            .build();
    }

    // ---- the command stream and check-ins, on the worker thread ----

    private void run() {
        long lastBeat = 0;
        while (running) {
            String base = Config.server(this) + "/api/devices/" + Config.deviceId(this);
            HttpURLConnection c = null;
            try {
                heartbeat();
                lastBeat = System.currentTimeMillis();
                c = (HttpURLConnection) new URL(base + "/stream").openConnection();
                c.setConnectTimeout(10_000);
                c.setReadTimeout(READ_TIMEOUT_MS);
                c.setRequestProperty("Accept", "text/event-stream");
                stream = c;
                BufferedReader in = new BufferedReader(new InputStreamReader(c.getInputStream(), StandardCharsets.UTF_8));
                String line;
                while (running && (line = in.readLine()) != null) {
                    if (line.startsWith("data:")) handle(base, line.substring(5).trim());
                    if (System.currentTimeMillis() - lastBeat >= HEARTBEAT_MS) {
                        heartbeat();
                        lastBeat = System.currentTimeMillis();
                    }
                }
            } catch (Exception ignored) {
                // network blip or server restart: reconnect below
            } finally {
                if (c != null) c.disconnect();
                stream = null;
            }
            if (!running) break;
            try {
                Thread.sleep(RETRY_MS);
            } catch (InterruptedException e) {
                break;
            }
        }
    }

    /** Only "display_on" is ours; anything else is left to the page, which may still be running. */
    private void handle(String base, String data) {
        String id, type;
        try {
            JSONObject cmd = new JSONObject(data);
            id = cmd.getString("id");
            type = cmd.optString("type");
        } catch (JSONException e) {
            return;
        }
        if (!"display_on".equals(type)) return;
        Launcher.wake(this);
        post(base + "/commands/" + id + "/ack", "{\"ok\":true,\"result\":\"waking\"}");
    }

    /** Check in with only what changes while asleep, so the server keeps what the page reported. */
    private void heartbeat() {
        JSONObject o = new JSONObject();
        try {
            o.put("id", Config.deviceId(this));
            o.put("client", "firetv-" + BuildInfo.VERSION);
            o.put("native", NativeBridge.describe(this));
        } catch (JSONException ignored) {
            return;
        }
        post(Config.server(this) + "/api/devices/heartbeat", o.toString());
    }

    private static void post(String url, String json) {
        HttpURLConnection c = null;
        try {
            byte[] body = json.getBytes(StandardCharsets.UTF_8);
            c = (HttpURLConnection) new URL(url).openConnection();
            c.setRequestMethod("POST");
            c.setDoOutput(true);
            c.setConnectTimeout(10_000);
            c.setReadTimeout(10_000);
            c.setRequestProperty("Content-Type", "application/json");
            c.setFixedLengthStreamingMode(body.length);
            try (OutputStream os = c.getOutputStream()) {
                os.write(body);
            }
            c.getResponseCode();
        } catch (Exception ignored) {
            // the next check-in tries again
        } finally {
            if (c != null) c.disconnect();
        }
    }
}
