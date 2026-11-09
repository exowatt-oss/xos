package com.example.xos.tv;

import android.app.AlarmManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.os.Build;
import android.os.PowerManager;
import android.os.SystemClock;
import android.provider.Settings;

/**
 * Keeps Xos TV the screen a stick is showing: opens it on boot / after an update, and brings it
 * back a few minutes after someone leaves it.
 *
 * Android 10+ (Fire OS 8) only lets an app open itself from the background when it holds
 * "display over other apps"; firetv/provision.sh grants it over ADB. Fire OS 7 needs nothing.
 */
final class Launcher {
    private static final int RETURN_REQUEST = 1;

    private Launcher() {}

    /** True when this device lets Xos TV open itself (boot, return timer). */
    static boolean canAutoStart(Context c) {
        return Build.VERSION.SDK_INT < Build.VERSION_CODES.Q || Settings.canDrawOverlays(c);
    }

    static void open(Context c) {
        Intent launch = new Intent(c, MainActivity.class);
        launch.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_REORDER_TO_FRONT);
        try {
            c.startActivity(launch);
        } catch (RuntimeException ignored) {
            // Blocked without the overlay permission on newer Fire OS; the user opens Xos TV once.
        }
    }

    /** Wake the stick and open Xos TV; with HDMI-CEC on, the TV turns on and switches to it. */
    @SuppressWarnings("deprecation")
    static void wake(Context c) {
        PowerManager pm = (PowerManager) c.getSystemService(Context.POWER_SERVICE);
        PowerManager.WakeLock wake = pm.newWakeLock(PowerManager.FULL_WAKE_LOCK
            | PowerManager.ACQUIRE_CAUSES_WAKEUP | PowerManager.ON_AFTER_RELEASE, "xos:display-on");
        wake.acquire(10_000);
        open(c);
    }

    /** Bring Xos TV back after {@code minutes}; replaces any earlier timer. */
    static void scheduleReturn(Context c, int minutes) {
        if (minutes <= 0) return;
        AlarmManager am = (AlarmManager) c.getSystemService(Context.ALARM_SERVICE);
        am.setAndAllowWhileIdle(AlarmManager.ELAPSED_REALTIME_WAKEUP,
            SystemClock.elapsedRealtime() + minutes * 60_000L, returnIntent(c));
    }

    static void cancelReturn(Context c) {
        ((AlarmManager) c.getSystemService(Context.ALARM_SERVICE)).cancel(returnIntent(c));
    }

    private static PendingIntent returnIntent(Context c) {
        Intent i = new Intent(c, BootReceiver.class).setAction(BootReceiver.ACTION_RETURN);
        return PendingIntent.getBroadcast(c, RETURN_REQUEST, i,
            PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
    }
}
