package com.madhmoun.jeebrelay;

import android.app.Notification;
import android.os.Bundle;
import android.service.notification.NotificationListenerService;
import android.service.notification.StatusBarNotification;
import android.util.Base64;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.LinkedHashSet;
import java.util.Set;

public final class JeebNotificationService extends NotificationListenerService {
    @Override
    public void onListenerConnected() {
        RelayConfig.status(this, "خدمة قراءة إشعارات جيب متصلة");
        if (PendingQueue.size(this) > 0) {
            RelayJobService.flushNow(this);
        }
    }

    @Override
    public void onNotificationPosted(StatusBarNotification item) {
        if (!RelayConfig.isComplete(this)) {
            return;
        }
        if (!RelayConfig.targetPackage(this).equals(item.getPackageName())) {
            return;
        }
        String raw = notificationText(item.getNotification());
        RelayConfig.prefs(this).edit()
                .putString(RelayConfig.KEY_LATEST_NOTIFICATION, raw)
                .apply();
        try {
            NotificationParser.Parsed parsed = NotificationParser.parse(
                    raw,
                    item.getPostTime(),
                    RelayConfig.value(this, RelayConfig.KEY_TX_REGEX, RelayConfig.DEFAULT_TX_REGEX),
                    RelayConfig.value(
                            this,
                            RelayConfig.KEY_AMOUNT_REGEX,
                            RelayConfig.DEFAULT_AMOUNT_REGEX
                    ),
                    RelayConfig.value(
                            this,
                            RelayConfig.KEY_SENDER_REGEX,
                            RelayConfig.DEFAULT_SENDER_REGEX
                    ),
                    RelayConfig.value(
                            this,
                            RelayConfig.KEY_SUCCESS_REGEX,
                            RelayConfig.DEFAULT_SUCCESS_REGEX
                    ),
                    RelayConfig.value(
                            this,
                            RelayConfig.KEY_DEBIT_REGEX,
                            RelayConfig.DEFAULT_DEBIT_REGEX
                    )
            );
            String payload = parsed.payload();
            String nonce = stableNonce(item, payload);
            if (PendingQueue.add(this, new RelayEvent(nonce, payload, System.currentTimeMillis()))) {
                RelayConfig.status(this, "تمت مطابقة إشعار جيب وحفظه للإرسال الآمن");
            }
            RelayJobService.flushNow(this);
        } catch (NotificationParser.ParseFailure failure) {
            RelayConfig.status(this, "آخر إشعار جيب لم يُرسل: " + failure.getMessage());
        } catch (Exception error) {
            RelayConfig.status(this, "تعذر تجهيز إشعار جيب؛ لم يُرسل شيء");
        }
    }

    private static String notificationText(Notification notification) {
        Bundle extras = notification.extras;
        Set<String> parts = new LinkedHashSet<>();
        add(parts, extras.getCharSequence(Notification.EXTRA_TITLE));
        add(parts, extras.getCharSequence(Notification.EXTRA_TEXT));
        add(parts, extras.getCharSequence(Notification.EXTRA_BIG_TEXT));
        add(parts, extras.getCharSequence(Notification.EXTRA_SUB_TEXT));
        CharSequence[] lines = extras.getCharSequenceArray(Notification.EXTRA_TEXT_LINES);
        if (lines != null) {
            for (CharSequence line : lines) {
                add(parts, line);
            }
        }
        return String.join("\n", parts);
    }

    private static void add(Set<String> parts, CharSequence value) {
        if (value == null) {
            return;
        }
        String text = value.toString().trim();
        if (!text.isEmpty()) {
            parts.add(text);
        }
    }

    private static String stableNonce(StatusBarNotification item, String payload) throws Exception {
        String source = item.getPackageName()
                + "\n" + item.getKey()
                + "\n" + item.getPostTime()
                + "\n" + payload;
        byte[] digest = MessageDigest.getInstance("SHA-256")
                .digest(source.getBytes(StandardCharsets.UTF_8));
        byte[] shortened = new byte[24];
        System.arraycopy(digest, 0, shortened, 0, shortened.length);
        return Base64.encodeToString(
                shortened,
                Base64.NO_WRAP | Base64.NO_PADDING | Base64.URL_SAFE
        );
    }
}
