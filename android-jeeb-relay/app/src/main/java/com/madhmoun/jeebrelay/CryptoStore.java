package com.madhmoun.jeebrelay;

import android.content.Context;
import android.content.SharedPreferences;
import android.os.Build;
import android.util.Base64;

final class CryptoStore {
    private static final String PREF_CIPHERTEXT = "relay_secret_ciphertext";
    private static final String PREF_IV = "relay_secret_iv";
    private static final String PREF_SCHEME = "relay_secret_scheme";
    private static final String SCHEME_AES_GCM = "aes-gcm-v1";
    private static final String SCHEME_RSA = "rsa-pkcs1-v1";

    private CryptoStore() {}

    static void saveSecret(Context context, String secret) throws Exception {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            CryptoStoreApi23.Encrypted value = CryptoStoreApi23.encrypt(secret);
            commit(context, value.ciphertext, value.iv, SCHEME_AES_GCM);
        } else {
            String ciphertext = CryptoStoreLegacy.encrypt(context, secret);
            commit(context, ciphertext, "", SCHEME_RSA);
        }
    }

    static String readSecret(Context context) {
        SharedPreferences prefs = RelayConfig.prefs(context);
        String encrypted = prefs.getString(PREF_CIPHERTEXT, "");
        String scheme = prefs.getString(PREF_SCHEME, "");
        if (encrypted.isEmpty() || scheme.isEmpty()) {
            return "";
        }
        try {
            if (SCHEME_AES_GCM.equals(scheme) && Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
                return CryptoStoreApi23.decrypt(
                        encrypted,
                        prefs.getString(PREF_IV, "")
                );
            }
            if (SCHEME_RSA.equals(scheme) && Build.VERSION.SDK_INT >= Build.VERSION_CODES.LOLLIPOP) {
                return CryptoStoreLegacy.decrypt(encrypted);
            }
            return "";
        } catch (Exception ignored) {
            return "";
        }
    }

    private static void commit(Context context, String ciphertext, String iv, String scheme) {
        boolean saved = RelayConfig.prefs(context)
                .edit()
                .putString(PREF_CIPHERTEXT, ciphertext)
                .putString(PREF_IV, iv)
                .putString(PREF_SCHEME, scheme)
                .commit();
        if (!saved) {
            throw new IllegalStateException("تعذر حفظ سر الجسر المشفّر");
        }
    }
}
