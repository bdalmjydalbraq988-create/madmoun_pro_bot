package com.madhmoun.jeebrelay;

import android.util.Base64;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.SecureRandom;

import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;

final class RelayProtocol {
    private static final SecureRandom RANDOM = new SecureRandom();

    private RelayProtocol() {}

    static String nonce() {
        byte[] bytes = new byte[24];
        RANDOM.nextBytes(bytes);
        return Base64.encodeToString(
                bytes,
                Base64.NO_WRAP | Base64.NO_PADDING | Base64.URL_SAFE
        );
    }

    static String randomSecret() {
        byte[] bytes = new byte[36];
        RANDOM.nextBytes(bytes);
        return Base64.encodeToString(
                bytes,
                Base64.NO_WRAP | Base64.NO_PADDING | Base64.URL_SAFE
        );
    }

    static String bodyHash(byte[] body) throws Exception {
        return hex(MessageDigest.getInstance("SHA-256").digest(body));
    }

    static String signature(
            String secret,
            byte[] body,
            String deviceId,
            long timestamp,
            String nonce
    ) throws Exception {
        String canonical = "jeeb-relay-v1\n"
                + deviceId + "\n"
                + timestamp + "\n"
                + nonce + "\n"
                + bodyHash(body);
        Mac mac = Mac.getInstance("HmacSHA256");
        mac.init(new SecretKeySpec(secret.getBytes(StandardCharsets.UTF_8), "HmacSHA256"));
        return hex(mac.doFinal(canonical.getBytes(StandardCharsets.UTF_8)));
    }

    private static String hex(byte[] value) {
        StringBuilder result = new StringBuilder(value.length * 2);
        for (byte item : value) {
            result.append(String.format("%02x", item & 0xff));
        }
        return result.toString();
    }
}
