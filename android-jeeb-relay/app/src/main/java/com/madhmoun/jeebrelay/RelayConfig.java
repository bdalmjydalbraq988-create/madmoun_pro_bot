package com.madhmoun.jeebrelay;

import android.content.Context;
import android.content.SharedPreferences;

final class RelayConfig {
    static final String PREFS = "relay_config_v1";
    static final String KEY_ENDPOINT = "endpoint";
    static final String KEY_DEVICE_ID = "device_id";
    static final String KEY_TARGET_PACKAGE = "target_package";
    static final String KEY_TX_REGEX = "tx_regex";
    static final String KEY_AMOUNT_REGEX = "amount_regex";
    static final String KEY_SENDER_REGEX = "sender_regex";
    static final String KEY_SUCCESS_REGEX = "success_regex";
    static final String KEY_DEBIT_REGEX = "debit_regex";
    static final String KEY_LATEST_NOTIFICATION = "latest_notification";
    static final String KEY_LAST_STATUS = "last_status";

    static final String DEFAULT_TX_REGEX =
            "(?iu)(?:رقم\\s*(?:العملية|المرجع)|مرجع\\s*العملية|transaction\\s*(?:id|no\\.?))"
                    + "\\s*[:：#-]?\\s*([A-Z0-9_-]{3,200})";
    static final String DEFAULT_AMOUNT_REGEX =
            "(?iu)(?:المبلغ|amount)\\s*[:：]?\\s*([0-9٠-٩٬,\\.]+)"
                    + "\\s*(?:ر\\.?\\s*ي|ريال(?:\\s*يمني)?|YER)";
    static final String DEFAULT_SENDER_REGEX =
            "(?iu)(?:من\\s*(?:حساب|رقم)?|المرسل|sender)\\s*[:：]?"
                    + "\\s*([0-9٠-٩ +()\\-]{6,40})";
    static final String DEFAULT_SUCCESS_REGEX =
            "(?iu)(?:تم\\s+(?:استلام|استقبال)|استلمت|حوالة\\s+واردة|"
                    + "تحويل\\s+وارد|received|credited)";
    static final String DEFAULT_DEBIT_REGEX =
            "(?iu)(?:تم\\s+(?:الإرسال|الدفع|السحب)|أرسلت|دفعت|سحب|خصم|"
                    + "sent|paid|withdraw)";

    private RelayConfig() {}

    static SharedPreferences prefs(Context context) {
        return context.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    static String value(Context context, String key, String fallback) {
        return prefs(context).getString(key, fallback);
    }

    static String endpoint(Context context) {
        return value(context, KEY_ENDPOINT, "").trim();
    }

    static String deviceId(Context context) {
        return value(context, KEY_DEVICE_ID, "").trim();
    }

    static String targetPackage(Context context) {
        return value(context, KEY_TARGET_PACKAGE, "").trim();
    }

    static boolean isComplete(Context context) {
        return endpoint(context).startsWith("https://")
                && !deviceId(context).isEmpty()
                && !targetPackage(context).isEmpty()
                && !CryptoStore.readSecret(context).isEmpty();
    }

    static void status(Context context, String value) {
        prefs(context).edit().putString(KEY_LAST_STATUS, value).apply();
    }
}
