package com.madhmoun.jeebrelay;

import android.content.Context;
import android.security.KeyPairGeneratorSpec;
import android.util.Base64;

import java.math.BigInteger;
import java.nio.charset.StandardCharsets;
import java.security.KeyPairGenerator;
import java.security.KeyStore;
import java.util.Calendar;

import javax.crypto.Cipher;
import javax.security.auth.x500.X500Principal;

/** Android 5 keystore implementation; the HMAC secret is never stored as plaintext. */
@SuppressWarnings("deprecation")
final class CryptoStoreLegacy {
    private static final String ANDROID_KEY_STORE = "AndroidKeyStore";
    private static final String KEY_ALIAS = "madhmoun_jeeb_relay_rsa_v1";

    private CryptoStoreLegacy() {}

    static String encrypt(Context context, String secret) throws Exception {
        KeyStore.PrivateKeyEntry entry = getOrCreateEntry(context);
        Cipher cipher = Cipher.getInstance("RSA/ECB/PKCS1Padding");
        cipher.init(Cipher.ENCRYPT_MODE, entry.getCertificate().getPublicKey());
        return Base64.encodeToString(
                cipher.doFinal(secret.getBytes(StandardCharsets.UTF_8)),
                Base64.NO_WRAP
        );
    }

    static String decrypt(String encrypted) throws Exception {
        KeyStore store = keyStore();
        KeyStore.Entry value = store.getEntry(KEY_ALIAS, null);
        if (!(value instanceof KeyStore.PrivateKeyEntry)) {
            return "";
        }
        Cipher cipher = Cipher.getInstance("RSA/ECB/PKCS1Padding");
        cipher.init(Cipher.DECRYPT_MODE, ((KeyStore.PrivateKeyEntry) value).getPrivateKey());
        byte[] clear = cipher.doFinal(Base64.decode(encrypted, Base64.NO_WRAP));
        return new String(clear, StandardCharsets.UTF_8);
    }

    private static KeyStore.PrivateKeyEntry getOrCreateEntry(Context context) throws Exception {
        KeyStore store = keyStore();
        KeyStore.Entry existing = store.getEntry(KEY_ALIAS, null);
        if (existing instanceof KeyStore.PrivateKeyEntry) {
            return (KeyStore.PrivateKeyEntry) existing;
        }
        Calendar start = Calendar.getInstance();
        Calendar end = Calendar.getInstance();
        end.add(Calendar.YEAR, 25);
        KeyPairGeneratorSpec spec = new KeyPairGeneratorSpec.Builder(context)
                .setAlias(KEY_ALIAS)
                .setSubject(new X500Principal("CN=Madhmoun Jeeb Relay"))
                .setSerialNumber(BigInteger.ONE)
                .setStartDate(start.getTime())
                .setEndDate(end.getTime())
                .build();
        KeyPairGenerator generator = KeyPairGenerator.getInstance("RSA", ANDROID_KEY_STORE);
        generator.initialize(spec);
        generator.generateKeyPair();
        KeyStore.Entry created = keyStore().getEntry(KEY_ALIAS, null);
        if (!(created instanceof KeyStore.PrivateKeyEntry)) {
            throw new IllegalStateException("تعذر إنشاء مفتاح التشفير الآمن");
        }
        return (KeyStore.PrivateKeyEntry) created;
    }

    private static KeyStore keyStore() throws Exception {
        KeyStore store = KeyStore.getInstance(ANDROID_KEY_STORE);
        store.load(null);
        return store;
    }
}
