package com.example.xos.tv;

import android.app.Activity;
import android.app.admin.DevicePolicyManager;
import android.content.ComponentName;
import android.content.Context;
import android.graphics.Bitmap;
import android.graphics.Canvas;
import android.os.Build;
import android.os.PowerManager;
import android.webkit.JavascriptInterface;
import android.webkit.WebView;

import org.json.JSONException;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.Inet4Address;
import java.net.InetAddress;
import java.net.NetworkInterface;
import java.net.URL;
import java.util.Collections;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

/**
 * window.XosNative for the Xos page: what a web page can't do on its own. The Xos server sends
 * commands (Admin → Devices) and the page calls these. Only pages from the configured Xos server
 * may use it; anything else gets "error: untrusted page".
 */
final class NativeBridge {
    interface Host {
        Activity activity();
        WebView web();
        String currentUrl();
        void reloadFromConfig();
    }

    private final Host host;
    private final Context app;

    NativeBridge(Host host) {
        this.host = host;
        this.app = host.activity().getApplicationContext();
    }

    private boolean trusted() {
        String url = host.currentUrl();
        return url != null && url.startsWith(Config.server(app) + "/");
    }

    private static boolean displayControl(Context app) {
        DevicePolicyManager dpm = (DevicePolicyManager) app.getSystemService(Context.DEVICE_POLICY_SERVICE);
        return dpm != null && dpm.isAdminActive(new ComponentName(app, AdminReceiver.class));
    }

    @JavascriptInterface
    public String info() {
        if (!trusted()) return "{}";
        return describe(app).toString();
    }

    /** What the server shows about this stick; also sent by SleepWatch while the stick sleeps. */
    static JSONObject describe(Context app) {
        PowerManager pm = (PowerManager) app.getSystemService(Context.POWER_SERVICE);
        JSONObject o = new JSONObject();
        try {
            o.put("manufacturer", Build.MANUFACTURER);
            o.put("model", Build.MODEL);
            o.put("os", fireOs() + "Android " + Build.VERSION.RELEASE + " (SDK " + Build.VERSION.SDK_INT + ")");
            o.put("app", BuildInfo.VERSION);
            o.put("ip", lanAddress());
            o.put("displayOn", pm != null && pm.isInteractive());
            o.put("displayControl", displayControl(app));
            o.put("wakeable", true); // stays reachable while asleep (SleepWatch), so Display off is safe
            o.put("autoStart", Launcher.canAutoStart(app));
            o.put("returnMinutes", Config.returnMinutes(app));
            o.put("route", Config.route(app));
            o.put("server", Config.server(app));
        } catch (JSONException ignored) {
            // all values are plain strings / booleans
        }
        return o;
    }

    /** Change and remember the screen, e.g. "#/signage/hq"; the stick reopens on it after a reboot. */
    @JavascriptInterface
    public String setRoute(String route) {
        if (!trusted()) return "error: untrusted page";
        if (route == null || !route.matches("#/[a-z]+(/[a-z0-9-]+){0,2}")) return "error: bad route";
        Config.setRoute(app, route);
        host.activity().runOnUiThread(host::reloadFromConfig);
        return "ok";
    }

    /** Remember the screen without reloading: the page switches to it itself, with no flash. */
    @JavascriptInterface
    public String saveRoute(String route) {
        if (!trusted()) return "error: untrusted page";
        if (route == null || !route.matches("#/[a-z]+(/[a-z0-9-]+){0,2}")) return "error: bad route";
        Config.setRoute(app, route);
        return "ok";
    }

    @JavascriptInterface
    public String reload() {
        if (!trusted()) return "error: untrusted page";
        host.activity().runOnUiThread(() -> {
            host.web().clearCache(true);
            host.reloadFromConfig();
        });
        return "reloading";
    }

    @JavascriptInterface
    public String setReturnMinutes(int minutes) {
        if (!trusted()) return "error: untrusted page";
        Config.setReturnMinutes(app, minutes);
        return "ok: " + Config.returnMinutes(app) + " min";
    }

    /** Sleep the stick; with HDMI-CEC on, the TV goes to standby too. Needs the device admin. */
    @JavascriptInterface
    public String displayOff() {
        if (!trusted()) return "error: untrusted page";
        if (!displayControl(app)) return "error: display control not enabled — run firetv/provision.sh";
        SleepWatch.start(app); // while Xos is still in front: keeps the stick reachable once asleep
        DevicePolicyManager dpm = (DevicePolicyManager) app.getSystemService(Context.DEVICE_POLICY_SERVICE);
        dpm.lockNow();
        return "ok";
    }

    /** Wake the stick; with HDMI-CEC on, the TV turns on and switches to this input. */
    @JavascriptInterface
    public String displayOn() {
        if (!trusted()) return "error: untrusted page";
        host.activity().runOnUiThread(() -> {
            if (Build.VERSION.SDK_INT >= 27) {
                host.activity().setTurnScreenOn(true);
                host.activity().setShowWhenLocked(true);
            }
        });
        Launcher.wake(app);
        return "ok";
    }

    /** Capture what the WebView shows and upload it as JPEG to {@code uploadUrl} (the Xos server). */
    @JavascriptInterface
    public String screenshot(String uploadUrl) {
        if (!trusted()) return "error: untrusted page";
        if (uploadUrl == null || !uploadUrl.startsWith(Config.server(app) + "/api/devices/")) return "error: bad upload url";
        final byte[][] jpeg = new byte[1][];
        CountDownLatch drawn = new CountDownLatch(1);
        host.activity().runOnUiThread(() -> {
            try {
                WebView web = host.web();
                int w = Math.max(1, web.getWidth()), h = Math.max(1, web.getHeight());
                Bitmap full = Bitmap.createBitmap(w, h, Bitmap.Config.ARGB_8888);
                web.draw(new Canvas(full));
                int tw = Math.min(w, 1280), th = Math.round(h * (tw / (float) w));
                Bitmap small = Bitmap.createScaledBitmap(full, tw, th, true);
                ByteArrayOutputStream out = new ByteArrayOutputStream();
                small.compress(Bitmap.CompressFormat.JPEG, 75, out);
                jpeg[0] = out.toByteArray();
                full.recycle();
                if (small != full) small.recycle();
            } finally {
                drawn.countDown();
            }
        });
        try {
            if (!drawn.await(5, TimeUnit.SECONDS) || jpeg[0] == null) return "error: capture failed";
        } catch (InterruptedException e) {
            return "error: interrupted";
        }
        new Thread(() -> upload(uploadUrl, jpeg[0]), "xos-screenshot").start();
        return "ok: " + (jpeg[0].length / 1024) + " KB uploading";
    }

    private static void upload(String url, byte[] body) {
        HttpURLConnection c = null;
        try {
            c = (HttpURLConnection) new URL(url).openConnection();
            c.setRequestMethod("POST");
            c.setDoOutput(true);
            c.setConnectTimeout(10_000);
            c.setReadTimeout(20_000);
            c.setRequestProperty("Content-Type", "image/jpeg");
            c.setFixedLengthStreamingMode(body.length);
            try (OutputStream os = c.getOutputStream()) {
                os.write(body);
            }
            c.getResponseCode();
        } catch (Exception ignored) {
            // the admin sees no new screenshot and can retry
        } finally {
            if (c != null) c.disconnect();
        }
    }

    /** "Fire OS 8.1.8.5 · " on Fire TV, else empty. */
    private static String fireOs() {
        try {
            Class<?> sp = Class.forName("android.os.SystemProperties");
            String name = (String) sp.getMethod("get", String.class).invoke(null, "ro.build.version.name");
            if (name != null && name.startsWith("Fire OS")) return name.split(" \\(")[0] + " · ";
        } catch (Exception ignored) {
            // not a Fire OS build
        }
        return "";
    }

    private static String lanAddress() {
        try {
            for (NetworkInterface nif : Collections.list(NetworkInterface.getNetworkInterfaces())) {
                if (!nif.isUp() || nif.isLoopback()) continue;
                for (InetAddress a : Collections.list(nif.getInetAddresses())) {
                    if (a instanceof Inet4Address) return a.getHostAddress();
                }
            }
        } catch (Exception ignored) {
            // no network yet
        }
        return "";
    }
}
