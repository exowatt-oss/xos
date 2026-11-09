package com.example.xos.tv;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/** Opens Xos TV after a reboot, after an app update, and when the return timer fires. */
public class BootReceiver extends BroadcastReceiver {
    static final String ACTION_RETURN = "com.example.xos.tv.RETURN";

    @Override
    public void onReceive(Context context, Intent intent) {
        String action = intent.getAction();
        boolean boot = Intent.ACTION_BOOT_COMPLETED.equals(action)
            || Intent.ACTION_MY_PACKAGE_REPLACED.equals(action);
        if (boot && Config.startOnBoot(context)) Launcher.open(context);
        else if (ACTION_RETURN.equals(action) && Config.returnMinutes(context) > 0) Launcher.open(context);
    }
}
