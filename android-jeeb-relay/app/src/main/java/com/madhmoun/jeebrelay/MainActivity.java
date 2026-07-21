package com.madhmoun.jeebrelay;

import android.app.Activity;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.content.pm.ResolveInfo;
import android.graphics.Color;
import android.os.Bundle;
import android.provider.Settings;
import android.text.InputType;
import android.view.View;
import android.view.WindowManager;
import android.widget.ArrayAdapter;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.Spinner;
import android.widget.TextView;
import android.widget.Toast;

import java.net.HttpURLConnection;
import java.net.URL;
import java.security.SecureRandom;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.Executors;
import java.util.regex.Pattern;

public final class MainActivity extends Activity {
    private EditText endpoint;
    private EditText deviceId;
    private EditText secret;
    private Spinner appSpinner;
    private EditText transactionRegex;
    private EditText amountRegex;
    private EditText senderRegex;
    private EditText successRegex;
    private EditText debitRegex;
    private TextView status;
    private TextView latest;
    private LinearLayout advanced;
    private List<AppChoice> apps;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        getWindow().setFlags(
                WindowManager.LayoutParams.FLAG_SECURE,
                WindowManager.LayoutParams.FLAG_SECURE
        );
        setTitle("Madhmoun Jeeb Relay");
        setContentView(buildView());
        load();
    }

    @Override
    protected void onResume() {
        super.onResume();
        refreshStatus();
    }

    private View buildView() {
        ScrollView scroll = new ScrollView(this);
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(dp(18), dp(18), dp(18), dp(36));
        root.setLayoutDirection(View.LAYOUT_DIRECTION_RTL);
        scroll.addView(root);

        TextView title = text("جسر جيب الآمن لمتجر مضمون", 24, true);
        root.addView(title);
        root.addView(text(
                "هذا التطبيق لا يدخل إلى حساب جيب ولا يعرف كلمة المرور. "
                        + "يقرأ إشعار التحويل الوارد من تطبيق جيب المحدد فقط ويرسله بتوقيع مشفّر.",
                15,
                false
        ));

        endpoint = field(root, "رابط الخادم HTTPS", "https://example.com/webhooks/jeeb-relay");
        deviceId = field(root, "هوية هذا الهاتف", "owner-phone-01");
        secret = field(root, "سر التوقيع (32 حرفًا على الأقل)", "");
        secret.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD);

        LinearLayout secretButtons = row();
        Button generate = button("توليد سر آمن");
        generate.setOnClickListener(view -> secret.setText(RelayProtocol.randomSecret()));
        Button copy = button("نسخ السر");
        copy.setOnClickListener(view -> copySecret());
        secretButtons.addView(generate, weight());
        secretButtons.addView(copy, weight());
        root.addView(secretButtons);

        root.addView(text("اختر تطبيق جيب الرسمي على هذا الهاتف", 15, true));
        appSpinner = new Spinner(this);
        root.addView(appSpinner, match());

        advanced = new LinearLayout(this);
        advanced.setOrientation(LinearLayout.VERTICAL);
        advanced.setVisibility(View.GONE);
        transactionRegex = field(advanced, "مطابقة رقم العملية", "");
        amountRegex = field(advanced, "مطابقة المبلغ", "");
        senderRegex = field(advanced, "مطابقة رقم المرسل", "");
        successRegex = field(advanced, "عبارة التحويل الوارد الناجح", "");
        debitRegex = field(advanced, "عبارات الدفع/السحب المرفوضة", "");
        Button advancedButton = button("إظهار إعدادات المطابقة المتقدمة");
        advancedButton.setOnClickListener(view -> {
            boolean show = advanced.getVisibility() != View.VISIBLE;
            advanced.setVisibility(show ? View.VISIBLE : View.GONE);
            advancedButton.setText(show ? "إخفاء الإعدادات المتقدمة" : "إظهار إعدادات المطابقة المتقدمة");
        });
        root.addView(advancedButton, match());
        root.addView(advanced);

        Button save = button("حفظ الإعدادات");
        save.setBackgroundColor(Color.rgb(21, 101, 192));
        save.setTextColor(Color.WHITE);
        save.setOnClickListener(view -> save());
        root.addView(save, matchTall());

        Button permission = button("منح صلاحية قراءة الإشعارات");
        permission.setOnClickListener(view -> openNotificationAccess());
        root.addView(permission, match());

        LinearLayout tests = row();
        Button health = button("اختبار الخادم");
        health.setOnClickListener(view -> testServer());
        Button parse = button("اختبار آخر إشعار");
        parse.setOnClickListener(view -> testLatest());
        tests.addView(health, weight());
        tests.addView(parse, weight());
        root.addView(tests);

        status = text("", 15, true);
        status.setTextColor(Color.rgb(27, 94, 32));
        root.addView(status);
        latest = text("لم يتم التقاط إشعار من تطبيق جيب بعد.", 13, false);
        latest.setTextIsSelectable(true);
        root.addView(latest);
        return scroll;
    }

    private void load() {
        SharedPreferences prefs = RelayConfig.prefs(this);
        endpoint.setText(prefs.getString(RelayConfig.KEY_ENDPOINT, ""));
        String id = prefs.getString(RelayConfig.KEY_DEVICE_ID, "");
        deviceId.setText(id.isEmpty() ? newDeviceId() : id);
        secret.setText(CryptoStore.readSecret(this));
        transactionRegex.setText(prefs.getString(
                RelayConfig.KEY_TX_REGEX,
                RelayConfig.DEFAULT_TX_REGEX
        ));
        amountRegex.setText(prefs.getString(
                RelayConfig.KEY_AMOUNT_REGEX,
                RelayConfig.DEFAULT_AMOUNT_REGEX
        ));
        senderRegex.setText(prefs.getString(
                RelayConfig.KEY_SENDER_REGEX,
                RelayConfig.DEFAULT_SENDER_REGEX
        ));
        successRegex.setText(prefs.getString(
                RelayConfig.KEY_SUCCESS_REGEX,
                RelayConfig.DEFAULT_SUCCESS_REGEX
        ));
        debitRegex.setText(prefs.getString(
                RelayConfig.KEY_DEBIT_REGEX,
                RelayConfig.DEFAULT_DEBIT_REGEX
        ));
        apps = loadLaunchableApps();
        ArrayAdapter<AppChoice> adapter = new ArrayAdapter<>(
                this,
                android.R.layout.simple_spinner_item,
                apps
        );
        adapter.setDropDownViewResource(android.R.layout.simple_spinner_dropdown_item);
        appSpinner.setAdapter(adapter);
        String savedPackage = prefs.getString(RelayConfig.KEY_TARGET_PACKAGE, "");
        int selected = 0;
        for (int index = 0; index < apps.size(); index++) {
            if (apps.get(index).packageName.equals(savedPackage)) {
                selected = index;
                break;
            }
            String label = apps.get(index).label.toLowerCase();
            if (savedPackage.isEmpty() && (label.contains("جيب") || label.contains("jaib"))) {
                selected = index;
            }
        }
        appSpinner.setSelection(selected);
    }

    private void save() {
        try {
            String normalizedEndpoint = normalizeEndpoint(endpoint.getText().toString());
            String id = deviceId.getText().toString().trim();
            String clearSecret = secret.getText().toString().trim();
            if (!id.matches("[A-Za-z0-9][A-Za-z0-9._-]{2,63}")) {
                throw new IllegalArgumentException("هوية الهاتف غير صالحة");
            }
            if (clearSecret.length() < 32) {
                throw new IllegalArgumentException("سر التوقيع يجب أن يكون 32 حرفًا على الأقل");
            }
            if (apps.isEmpty() || appSpinner.getSelectedItem() == null) {
                throw new IllegalArgumentException("اختر تطبيق جيب");
            }
            requireCapture(transactionRegex.getText().toString(), "رقم العملية");
            requireCapture(amountRegex.getText().toString(), "المبلغ");
            requireCapture(senderRegex.getText().toString(), "رقم المرسل");
            Pattern.compile(successRegex.getText().toString());
            Pattern.compile(debitRegex.getText().toString());
            AppChoice choice = (AppChoice) appSpinner.getSelectedItem();
            if (choice.packageName.isEmpty()) {
                throw new IllegalArgumentException("اختر تطبيق جيب الرسمي من القائمة");
            }
            boolean committed = RelayConfig.prefs(this).edit()
                    .putString(RelayConfig.KEY_ENDPOINT, normalizedEndpoint)
                    .putString(RelayConfig.KEY_DEVICE_ID, id)
                    .putString(RelayConfig.KEY_TARGET_PACKAGE, choice.packageName)
                    .putString(RelayConfig.KEY_TX_REGEX, transactionRegex.getText().toString())
                    .putString(RelayConfig.KEY_AMOUNT_REGEX, amountRegex.getText().toString())
                    .putString(RelayConfig.KEY_SENDER_REGEX, senderRegex.getText().toString())
                    .putString(RelayConfig.KEY_SUCCESS_REGEX, successRegex.getText().toString())
                    .putString(RelayConfig.KEY_DEBIT_REGEX, debitRegex.getText().toString())
                    .commit();
            if (!committed) {
                throw new IllegalStateException("تعذر حفظ الإعدادات");
            }
            CryptoStore.saveSecret(this, clearSecret);
            endpoint.setText(normalizedEndpoint);
            RelayConfig.status(this, "تم حفظ الإعدادات بأمان");
            RelayJobService.flushNow(this);
            refreshStatus();
            toast("تم الحفظ");
        } catch (Exception error) {
            toast(error.getMessage() == null ? "الإعدادات غير صحيحة" : error.getMessage());
        }
    }

    private void testLatest() {
        String raw = RelayConfig.prefs(this).getString(RelayConfig.KEY_LATEST_NOTIFICATION, "");
        if (raw.isEmpty()) {
            toast("لا يوجد إشعار جيب ملتقط بعد");
            return;
        }
        try {
            NotificationParser.Parsed parsed = NotificationParser.parse(
                    raw,
                    System.currentTimeMillis(),
                    transactionRegex.getText().toString(),
                    amountRegex.getText().toString(),
                    senderRegex.getText().toString(),
                    successRegex.getText().toString(),
                    debitRegex.getText().toString()
            );
            latest.setText(
                    "✅ المطابقة صحيحة محليًا\nرقم العملية: " + parsed.transactionId
                            + "\nالمبلغ: " + parsed.amount + " YER"
                            + "\nالمرسل: " + parsed.sender
            );
        } catch (Exception error) {
            latest.setText("❌ فشلت المطابقة: " + error.getMessage() + "\n\n" + raw);
        }
    }

    private void testServer() {
        String relayUrl;
        try {
            relayUrl = normalizeEndpoint(endpoint.getText().toString());
        } catch (Exception error) {
            toast(error.getMessage());
            return;
        }
        status.setText("جارٍ اختبار الخادم…");
        Executors.newSingleThreadExecutor().execute(() -> {
            HttpURLConnection connection = null;
            String result;
            try {
                connection = (HttpURLConnection) new URL(relayUrl + "/health").openConnection();
                connection.setInstanceFollowRedirects(false);
                connection.setConnectTimeout(12000);
                connection.setReadTimeout(12000);
                int code = connection.getResponseCode();
                result = code == 200
                        ? "✅ الخادم متصل. احفظ الإعدادات ثم فعّل قراءة الإشعارات."
                        : "❌ رد الخادم HTTP " + code;
            } catch (Exception error) {
                result = "❌ تعذر الوصول إلى الخادم عبر HTTPS";
            } finally {
                if (connection != null) {
                    connection.disconnect();
                }
            }
            String finalResult = result;
            runOnUiThread(() -> status.setText(finalResult));
        });
    }

    private void refreshStatus() {
        if (status == null) {
            return;
        }
        String listeners = Settings.Secure.getString(
                getContentResolver(),
                "enabled_notification_listeners"
        );
        boolean permission = listeners != null && listeners.contains(getPackageName());
        int pending = PendingQueue.size(this);
        String last = RelayConfig.prefs(this).getString(RelayConfig.KEY_LAST_STATUS, "");
        status.setText(
                (permission ? "✅ صلاحية الإشعارات مفعلة" : "⚠️ صلاحية الإشعارات غير مفعلة")
                        + "\nالإشعارات المعلقة: " + pending
                        + (last.isEmpty() ? "" : "\n" + last)
        );
        String raw = RelayConfig.prefs(this).getString(RelayConfig.KEY_LATEST_NOTIFICATION, "");
        if (!raw.isEmpty()) {
            latest.setText("آخر إشعار ملتقط (محفوظ على الهاتف فقط):\n" + raw);
        }
    }

    private List<AppChoice> loadLaunchableApps() {
        PackageManager manager = getPackageManager();
        Intent launcher = new Intent(Intent.ACTION_MAIN);
        launcher.addCategory(Intent.CATEGORY_LAUNCHER);
        List<ResolveInfo> found = manager.queryIntentActivities(launcher, 0);
        Map<String, AppChoice> unique = new LinkedHashMap<>();
        for (ResolveInfo info : found) {
            String packageName = info.activityInfo.packageName;
            String label = info.loadLabel(manager).toString();
            unique.put(packageName, new AppChoice(label, packageName));
        }
        List<AppChoice> result = new ArrayList<>(unique.values());
        Collections.sort(result, new Comparator<AppChoice>() {
            @Override
            public int compare(AppChoice left, AppChoice right) {
                return left.label.compareToIgnoreCase(right.label);
            }
        });
        result.add(0, new AppChoice("— اختر تطبيق جيب الرسمي —", ""));
        return result;
    }

    private static String normalizeEndpoint(String raw) throws Exception {
        String value = raw.trim();
        while (value.endsWith("/")) {
            value = value.substring(0, value.length() - 1);
        }
        if (!value.startsWith("https://")) {
            throw new IllegalArgumentException("رابط الخادم يجب أن يبدأ بـ https://");
        }
        if (!value.endsWith("/webhooks/jeeb-relay")) {
            value += "/webhooks/jeeb-relay";
        }
        URL parsed = new URL(value);
        if (parsed.getHost().isEmpty() || parsed.getUserInfo() != null || parsed.getQuery() != null) {
            throw new IllegalArgumentException("رابط الخادم غير صالح");
        }
        return value;
    }

    private static void requireCapture(String expression, String label) {
        Pattern pattern = Pattern.compile(expression);
        if (pattern.matcher("").groupCount() < 1) {
            throw new IllegalArgumentException("مطابقة " + label + " يجب أن تحتوي مجموعة استخراج ()");
        }
    }

    private void copySecret() {
        String value = secret.getText().toString();
        if (value.length() < 32) {
            toast("ولّد سرًا آمنًا أولًا");
            return;
        }
        ClipboardManager clipboard = (ClipboardManager) getSystemService(
                CLIPBOARD_SERVICE
        );
        if (clipboard == null) {
            toast("تعذر فتح الحافظة على هذا الهاتف");
            return;
        }
        clipboard.setPrimaryClip(ClipData.newPlainText("JEEB_RELAY_SECRET", value));
        toast("تم نسخ السر؛ الصقه في متغير الخادم ثم امسح الحافظة");
    }

    private void openNotificationAccess() {
        Intent direct = new Intent("android.settings.ACTION_NOTIFICATION_LISTENER_SETTINGS");
        if (direct.resolveActivity(getPackageManager()) != null) {
            startActivity(direct);
            return;
        }
        toast("افتح وصول الإشعارات ثم فعّل Madhmoun Jeeb Relay V3");
        startActivity(new Intent(Settings.ACTION_SETTINGS));
    }

    private String newDeviceId() {
        int value = new SecureRandom().nextInt();
        return "owner-phone-" + String.format("%08x", value);
    }

    private EditText field(LinearLayout parent, String label, String hint) {
        parent.addView(text(label, 14, true));
        EditText result = new EditText(this);
        result.setHint(hint);
        result.setTextDirection(View.TEXT_DIRECTION_LTR);
        result.setSingleLine(false);
        result.setMinLines(1);
        result.setPadding(dp(12), dp(10), dp(12), dp(10));
        parent.addView(result, match());
        return result;
    }

    private TextView text(String value, int size, boolean bold) {
        TextView result = new TextView(this);
        result.setText(value);
        result.setTextSize(size);
        result.setTextColor(Color.rgb(32, 33, 36));
        result.setPadding(0, dp(8), 0, dp(8));
        if (bold) {
            result.setTypeface(result.getTypeface(), android.graphics.Typeface.BOLD);
        }
        return result;
    }

    private LinearLayout row() {
        LinearLayout result = new LinearLayout(this);
        result.setOrientation(LinearLayout.HORIZONTAL);
        return result;
    }

    private Button button(String label) {
        Button result = new Button(this);
        result.setText(label);
        result.setAllCaps(false);
        return result;
    }

    private LinearLayout.LayoutParams match() {
        return new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT
        );
    }

    private LinearLayout.LayoutParams matchTall() {
        LinearLayout.LayoutParams params = match();
        params.topMargin = dp(12);
        params.bottomMargin = dp(8);
        return params;
    }

    private LinearLayout.LayoutParams weight() {
        return new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1);
    }

    private int dp(int value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }

    private void toast(String message) {
        Toast.makeText(this, message, Toast.LENGTH_LONG).show();
    }

    private static final class AppChoice {
        final String label;
        final String packageName;

        AppChoice(String label, String packageName) {
            this.label = label;
            this.packageName = packageName;
        }

        @Override
        public String toString() {
            return label + " — " + packageName;
        }
    }
}
