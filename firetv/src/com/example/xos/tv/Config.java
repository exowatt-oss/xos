package com.example.xos.tv;

import android.content.Context;
import android.content.SharedPreferences;

/** Which Xos screen this Fire Stick shows, and where it loads it from. */
final class Config {
    /** The default Xos server: set it to yours before building: live sync with every controller, panel and screen. */
    static final String CLOUD = "https://xos.example.org";

    /** Label and route for every screen the TV can show. Routes match app/js/main.js kiosk views. */
    static final String[][] SCREENS = {
        {"Setup · show pairing code", "#/setup"},
        {"Room TV · Lamar", "#/tv/lamar"},
        {"Door panel · Lamar", "#/panel/lamar"},
        {"Lobby signage · HQ", "#/signage/hq"},
        {"Lobby signage · Annex", "#/signage/annex"},
        {"Dashboard wall", "#/wall"},
    };

    private static final String PREFS = "xos";
    private static final String K_SERVER = "server";
    private static final String K_ROUTE = "route";
    private static final String K_URL = "url";
    private static final String K_BOOT = "boot";
    private static final String K_DEVICE = "device";
    private static final String K_RETURN = "return_min";

    /** Choices for "return to Xos after leaving it" in minutes; 0 = off. */
    static final int[] RETURN_CHOICES = {0, 1, 2, 5, 15};

    private Config() {}

    private static SharedPreferences prefs(Context c) {
        return c.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    /** Base URL: the Xos server, or a Pi hub on the LAN such as http://192.0.2.40:7000. */
    static String server(Context c) {
        return prefs(c).getString(K_SERVER, CLOUD);
    }

    static String route(Context c) {
        // New installs show the setup screen's pairing code until Admin → Devices assigns a screen.
        return prefs(c).getString(K_ROUTE, "#/setup");
    }

    /** A full custom URL overrides server + route. */
    static String customUrl(Context c) {
        return prefs(c).getString(K_URL, "");
    }

    static boolean startOnBoot(Context c) {
        return prefs(c).getBoolean(K_BOOT, true);
    }

    static String url(Context c) {
        String custom = customUrl(c);
        if (!custom.isEmpty()) return custom;
        return trimSlash(server(c)) + "/app/?kiosk=1&device=" + deviceId(c)
            + "&client=firetv-" + BuildInfo.VERSION + route(c);
    }

    /** Stable per-install id the server uses to list this stick under Admin → Devices. */
    static String deviceId(Context c) {
        String id = prefs(c).getString(K_DEVICE, "");
        if (id.isEmpty()) {
            id = "firetv-" + java.util.UUID.randomUUID().toString().substring(0, 8);
            prefs(c).edit().putString(K_DEVICE, id).apply();
        }
        return id;
    }

    static void setServer(Context c, String server) {
        prefs(c).edit().putString(K_SERVER, normalizeServer(server)).remove(K_URL).apply();
    }

    static void setRoute(Context c, String route) {
        prefs(c).edit().putString(K_ROUTE, route).remove(K_URL).apply();
    }

    static void setCustomUrl(Context c, String url) {
        prefs(c).edit().putString(K_URL, url.trim()).apply();
    }

    /** Minutes after someone leaves Xos TV (Home, another app) before it comes back. 0 = never. */
    static int returnMinutes(Context c) {
        return prefs(c).getInt(K_RETURN, 2);
    }

    static void setReturnMinutes(Context c, int minutes) {
        prefs(c).edit().putInt(K_RETURN, Math.max(0, minutes)).apply();
    }

    static void setStartOnBoot(Context c, boolean on) {
        prefs(c).edit().putBoolean(K_BOOT, on).apply();
    }

    /** "192.0.2.40" → "http://192.0.2.40:7000" (the xos-agent default port). */
    static String normalizeServer(String s) {
        s = s.trim();
        if (s.isEmpty()) return CLOUD;
        if (!s.startsWith("http://") && !s.startsWith("https://")) {
            s = "http://" + s;
            String hostPart = s.substring("http://".length());
            if (!hostPart.contains(":") && !hostPart.contains("/")) s += ":7000";
        }
        return trimSlash(s);
    }

    private static String trimSlash(String s) {
        while (s.endsWith("/")) s = s.substring(0, s.length() - 1);
        return s;
    }
}
