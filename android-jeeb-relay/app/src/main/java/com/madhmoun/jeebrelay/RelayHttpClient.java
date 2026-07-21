package com.madhmoun.jeebrelay;

import android.content.Context;

import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.List;

final class RelayHttpClient {
    enum Outcome { DELIVERED, RETRY, CONFIGURATION_ERROR }

    private RelayHttpClient() {}

    static boolean flush(Context context) {
        if (!RelayConfig.isComplete(context)) {
            RelayConfig.status(context, "الإعدادات غير مكتملة؛ لم يتم إرسال أي عملية");
            return false;
        }
        List<RelayEvent> pending = PendingQueue.read(context);
        boolean retryNeeded = false;
        for (RelayEvent event : pending) {
            Outcome outcome = send(context, event);
            if (outcome == Outcome.DELIVERED) {
                PendingQueue.remove(context, event.nonce);
            } else if (outcome == Outcome.CONFIGURATION_ERROR) {
                return false;
            } else {
                retryNeeded = true;
            }
        }
        if (!retryNeeded && PendingQueue.size(context) == 0) {
            RelayConfig.status(context, "تم إرسال جميع إشعارات جيب الموثوقة");
        }
        return retryNeeded;
    }

    static Outcome send(Context context, RelayEvent event) {
        HttpURLConnection connection = null;
        try {
            URL url = new URL(RelayConfig.endpoint(context));
            if (!"https".equalsIgnoreCase(url.getProtocol())) {
                RelayConfig.status(context, "تم رفض الرابط: يجب أن يبدأ بـ https://");
                return Outcome.CONFIGURATION_ERROR;
            }
            byte[] body = event.payload.getBytes(StandardCharsets.UTF_8);
            long timestamp = System.currentTimeMillis() / 1000L;
            String signature = RelayProtocol.signature(
                    CryptoStore.readSecret(context),
                    body,
                    RelayConfig.deviceId(context),
                    timestamp,
                    event.nonce
            );
            connection = (HttpURLConnection) url.openConnection();
            connection.setInstanceFollowRedirects(false);
            connection.setConnectTimeout(15000);
            connection.setReadTimeout(15000);
            connection.setRequestMethod("POST");
            connection.setDoOutput(true);
            connection.setFixedLengthStreamingMode(body.length);
            connection.setRequestProperty("Content-Type", "application/json; charset=utf-8");
            connection.setRequestProperty("Accept", "application/json");
            connection.setRequestProperty("X-Jeeb-Relay-Version", "1");
            connection.setRequestProperty("X-Jeeb-Device-Id", RelayConfig.deviceId(context));
            connection.setRequestProperty("X-Jeeb-Timestamp", Long.toString(timestamp));
            connection.setRequestProperty("X-Jeeb-Nonce", event.nonce);
            connection.setRequestProperty("X-Jeeb-Signature", signature);
            connection.getOutputStream().write(body);
            int status = connection.getResponseCode();
            drain(status >= 400 ? connection.getErrorStream() : connection.getInputStream());
            if (status >= 200 && status < 300) {
                RelayConfig.status(context, "تم تسليم إشعار جيب للخادم بنجاح");
                return Outcome.DELIVERED;
            }
            if (status == 400 || status == 409 || status == 422) {
                RelayConfig.status(
                        context,
                        "استلم الخادم الإشعار لكنه رفض المطابقة (HTTP " + status + ")؛ راجع الطلب"
                );
                return Outcome.DELIVERED;
            }
            if (status == 401 || status == 403 || status == 404) {
                RelayConfig.status(context, "خطأ في رابط/هوية/سر الجسر (HTTP " + status + ")");
                return Outcome.CONFIGURATION_ERROR;
            }
            RelayConfig.status(context, "الخادم غير متاح مؤقتًا (HTTP " + status + ")");
            return Outcome.RETRY;
        } catch (Exception error) {
            RelayConfig.status(context, "تعذر الاتصال؛ ستتم إعادة المحاولة تلقائيًا");
            return Outcome.RETRY;
        } finally {
            if (connection != null) {
                connection.disconnect();
            }
        }
    }

    private static void drain(InputStream stream) {
        if (stream == null) {
            return;
        }
        try (InputStream input = stream) {
            byte[] buffer = new byte[1024];
            int total = 0;
            int count;
            while (total < 8192 && (count = input.read(buffer)) >= 0) {
                total += count;
            }
        } catch (Exception ignored) {
            // The HTTP status is authoritative; response text never contains needed state.
        }
    }
}
