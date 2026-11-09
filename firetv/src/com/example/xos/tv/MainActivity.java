package com.example.xos.tv;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.graphics.Color;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.PowerManager;
import android.provider.Settings;
import android.text.InputType;
import android.view.KeyEvent;
import android.view.View;
import android.view.WindowManager;
import android.webkit.RenderProcessGoneDetail;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.EditText;

/**
 * Xos TV: shows one Xos screen full-screen on a Fire TV Stick.
 *
 * Remote: Menu (≡) or Back opens settings. Everything else goes to the page.
 * Setup over ADB: firetv/provision.sh, or
 *   adb shell am start -n com.example.xos.tv/.MainActivity --es route "#/tv/lamar" [--es server 192.0.2.40] [--ei return_min 2]
 */
public class MainActivity extends Activity implements NativeBridge.Host {
    private static final long RETRY_MS = 15_000;
    private static final long MENU_TIMEOUT_MS = 60_000;

    private final Handler handler = new Handler(Looper.getMainLooper());
    private final Runnable retry = this::load;
    private WebView web;
    private AlertDialog dialog;
    private volatile String currentUrl;
    private final Runnable closeMenu = () -> { if (dialog != null) dialog.dismiss(); };

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);

        web = new WebView(this);
        web.setBackgroundColor(Color.BLACK);
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_COMPATIBILITY_MODE);
        s.setUseWideViewPort(true);
        s.setLoadWithOverviewMode(true);
        s.setUserAgentString(s.getUserAgentString() + " XosTV/" + BuildInfo.VERSION);
        web.setWebChromeClient(new WebChromeClient());
        web.addJavascriptInterface(new NativeBridge(this), "XosNative");
        web.setWebViewClient(new WebViewClient() {
            @Override
            public void onPageStarted(WebView view, String url, android.graphics.Bitmap favicon) {
                currentUrl = url;
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest req, WebResourceError err) {
                if (req.isForMainFrame()) showOffline(String.valueOf(err.getDescription()));
            }

            @Override
            public boolean onRenderProcessGone(WebView view, RenderProcessGoneDetail detail) {
                recreate(); // the page crashed or was killed for memory: start over instead of a blank TV
                return true;
            }
        });
        setContentView(web);

        applyIntent(getIntent());
        load();
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        if (applyIntent(intent)) load();
    }

    /** ADB / automation: --es server, --es route, --es url, --ez boot. Returns true if anything changed. */
    private boolean applyIntent(Intent i) {
        if (i == null || i.getExtras() == null) return false;
        boolean changed = false;
        if (i.hasExtra("server")) { Config.setServer(this, i.getStringExtra("server")); changed = true; }
        if (i.hasExtra("route")) { Config.setRoute(this, i.getStringExtra("route")); changed = true; }
        if (i.hasExtra("url")) { Config.setCustomUrl(this, i.getStringExtra("url")); changed = true; }
        if (i.hasExtra("boot")) { Config.setStartOnBoot(this, i.getBooleanExtra("boot", true)); changed = true; }
        if (i.hasExtra("return_min")) { Config.setReturnMinutes(this, i.getIntExtra("return_min", 2)); changed = true; }
        return changed;
    }

    // NativeBridge.Host
    @Override public Activity activity() { return this; }
    @Override public WebView web() { return web; }
    @Override public String currentUrl() { return currentUrl; }
    @Override public void reloadFromConfig() { load(); }

    @Override
    protected void onResume() {
        super.onResume();
        Launcher.cancelReturn(this);
        SleepWatch.stop(this); // back on screen: the page checks in and hears commands again
    }

    @Override
    protected void onStop() {
        super.onStop();
        PowerManager pm = (PowerManager) getSystemService(POWER_SERVICE);
        if (pm != null && !pm.isInteractive()) {
            // The stick is going to sleep (Display off, or the TV turned off over HDMI-CEC): stay
            // reachable so Xos can wake it. Coming back on a timer would turn the TV on again.
            SleepWatch.start(this);
            return;
        }
        // Someone pressed Home or opened another app: come back to the room screen after a while.
        Launcher.scheduleReturn(this, Config.returnMinutes(this));
    }

    private void load() {
        handler.removeCallbacks(retry);
        String url = Config.url(this);
        // Same URL (it differs only by #route) would be a same-document hash change: force a real reload.
        if (url.equals(web.getUrl())) web.reload();
        else web.loadUrl(url);
    }

    private void showOffline(String reason) {
        String html = "<html><body style='margin:0;height:100vh;display:flex;align-items:center;justify-content:center;"
            + "background:#000;color:#fff;font-family:sans-serif;text-align:center'><div>"
            + "<div style='font-size:42px;font-weight:700'>Xos TV</div>"
            + "<p style='font-size:22px;color:#CDD0D5'>Can't reach " + escape(Config.url(this)) + "</p>"
            + "<p style='font-size:18px;color:#868C98'>" + escape(reason) + " &middot; retrying every 15 s &middot; press Menu for settings</p>"
            + "</div></body></html>";
        web.loadDataWithBaseURL(null, html, "text/html", "utf-8", null);
        handler.postDelayed(retry, RETRY_MS);
    }

    private static String escape(String s) {
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;");
    }

    @Override
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        if (keyCode == KeyEvent.KEYCODE_MENU || keyCode == KeyEvent.KEYCODE_BACK) {
            showMenu();
            return true;
        }
        return super.onKeyDown(keyCode, event);
    }

    @Override
    public void onWindowFocusChanged(boolean hasFocus) {
        super.onWindowFocusChanged(hasFocus);
        if (hasFocus) {
            getWindow().getDecorView().setSystemUiVisibility(
                View.SYSTEM_UI_FLAG_FULLSCREEN | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                    | View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY | View.SYSTEM_UI_FLAG_LAYOUT_STABLE
                    | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN);
        }
    }

    // ---- Settings (driven entirely by the Fire TV remote) ----

    private AlertDialog.Builder builder() {
        return new AlertDialog.Builder(this, android.R.style.Theme_DeviceDefault_Dialog_Alert);
    }

    private void show(AlertDialog.Builder b) {
        if (dialog != null && dialog.isShowing()) dialog.dismiss();
        dialog = b.show();
        // A menu left open would cover the room screen indefinitely.
        handler.removeCallbacks(closeMenu);
        handler.postDelayed(closeMenu, MENU_TIMEOUT_MS);
    }

    private void showMenu() {
        String server = Config.server(this);
        String[] items = {
            "Screen: " + screenLabel(),
            "Source: " + (Config.CLOUD.equals(server) ? "Xos server · xos.example.org" : server),
            "Custom URL…",
            "Start on boot: " + (Config.startOnBoot(this) ? "on" : "off"),
            "Return to Xos after leaving: " + returnLabel(Config.returnMinutes(this)),
            "Auto-start permission: " + (Launcher.canAutoStart(this) ? "granted" : "needed — select to allow"),
            "Reload",
            "Exit Xos TV" + (Config.returnMinutes(this) > 0 ? " (returns in " + returnLabel(Config.returnMinutes(this)) + ")" : ""),
        };
        show(builder().setTitle("Xos TV · " + Config.url(this)).setItems(items, (d, which) -> {
            switch (which) {
                case 0: pickScreen(); break;
                case 1: pickSource(); break;
                case 2: askText("Custom URL", Config.url(this), InputType.TYPE_TEXT_VARIATION_URI, v -> {
                    Config.setCustomUrl(this, v); load(); });
                    break;
                case 3: Config.setStartOnBoot(this, !Config.startOnBoot(this)); showMenu(); break;
                case 4: Config.setReturnMinutes(this, nextReturn(Config.returnMinutes(this))); showMenu(); break;
                case 5: requestAutoStart(); break;
                case 6: web.clearCache(true); web.loadUrl("about:blank"); load(); break;
                default: finish();
            }
        }));
    }

    private static String returnLabel(int minutes) {
        return minutes <= 0 ? "off" : minutes + " min";
    }

    private static int nextReturn(int current) {
        int[] c = Config.RETURN_CHOICES;
        for (int i = 0; i < c.length; i++) if (c[i] == current) return c[(i + 1) % c.length];
        return c[0];
    }

    /** Open Android's "display over other apps" screen; Fire OS may hide it, so provision.sh grants it over ADB. */
    private void requestAutoStart() {
        if (Launcher.canAutoStart(this)) { showMenu(); return; }
        try {
            startActivity(new Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
                android.net.Uri.parse("package:" + getPackageName())));
        } catch (RuntimeException e) {
            show(builder().setTitle("Auto-start permission")
                .setMessage("This Fire TV hides the setting. From a laptop on the same network run:\n\n"
                    + "firetv/provision.sh <fire-tv-ip>\n\nXos TV still works; you just open it once after a power cut.")
                .setPositiveButton("OK", null));
        }
    }

    private String screenLabel() {
        if (!Config.customUrl(this).isEmpty()) return "Custom URL";
        String route = Config.route(this);
        for (String[] s : Config.SCREENS) if (s[1].equals(route)) return s[0];
        return route;
    }

    private void pickScreen() {
        String[] labels = new String[Config.SCREENS.length];
        for (int i = 0; i < labels.length; i++) labels[i] = Config.SCREENS[i][0];
        show(builder().setTitle("Show which screen?").setItems(labels, (d, which) -> {
            Config.setRoute(this, Config.SCREENS[which][1]);
            load();
        }));
    }

    private void pickSource() {
        String[] items = {
            "Xos server · xos.example.org (recommended)",
            "Pi hub on this network…",
        };
        show(builder().setTitle("Load Xos from").setItems(items, (d, which) -> {
            if (which == 0) { Config.setServer(this, Config.CLOUD); load(); return; }
            String current = Config.server(this);
            askText("Hub address (e.g. 192.0.2.40)", Config.CLOUD.equals(current) ? "" : current,
                InputType.TYPE_TEXT_VARIATION_URI, v -> { Config.setServer(this, v); load(); });
        }));
    }

    private interface OnText { void accept(String value); }

    private void askText(String title, String initial, int variation, OnText done) {
        EditText input = new EditText(this);
        input.setInputType(InputType.TYPE_CLASS_TEXT | variation);
        input.setSingleLine(true);
        input.setText(initial);
        input.setSelection(input.getText().length());
        show(builder().setTitle(title).setView(input)
            .setPositiveButton("Save", (d, w) -> done.accept(input.getText().toString()))
            .setNegativeButton("Cancel", null));
    }

    @Override
    protected void onDestroy() {
        handler.removeCallbacks(retry);
        handler.removeCallbacks(closeMenu);
        if (dialog != null) dialog.dismiss();
        web.destroy();
        super.onDestroy();
    }
}
