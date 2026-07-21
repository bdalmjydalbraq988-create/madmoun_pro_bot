package com.madhmoun.jeebrelay;

import org.json.JSONObject;

import java.math.BigDecimal;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;
import java.util.TimeZone;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

final class NotificationParser {
    private NotificationParser() {}

    static Parsed parse(
            String raw,
            long occurredAtMillis,
            String transactionPattern,
            String amountPattern,
            String senderPattern,
            String successPattern,
            String debitPattern
    ) throws ParseFailure {
        String compact = raw == null ? "" : raw.replace('\u0000', ' ').trim();
        if (compact.length() < 8 || compact.length() > 8000) {
            throw new ParseFailure("طول الإشعار غير صالح");
        }
        if (!Pattern.compile(successPattern).matcher(compact).find()) {
            throw new ParseFailure("الإشعار ليس تحويلًا واردًا ناجحًا");
        }
        if (Pattern.compile(debitPattern).matcher(compact).find()) {
            throw new ParseFailure("تم رفض إشعار خصم/دفع صادر");
        }

        String transactionId = capture(transactionPattern, compact, "رقم العملية")
                .trim()
                .toUpperCase(Locale.ROOT);
        if (!transactionId.matches("[A-Z0-9_-]{3,200}")) {
            throw new ParseFailure("رقم العملية غير صالح");
        }
        String amountText = toWesternDigits(capture(amountPattern, compact, "المبلغ"))
                .replace("٬", "")
                .replace(",", "")
                .trim();
        BigDecimal amount;
        try {
            amount = new BigDecimal(amountText).stripTrailingZeros();
        } catch (NumberFormatException error) {
            throw new ParseFailure("المبلغ غير صالح");
        }
        if (amount.signum() <= 0 || amount.compareTo(new BigDecimal("1000000000000")) > 0) {
            throw new ParseFailure("المبلغ خارج النطاق المسموح");
        }
        String sender = onlyDigits(capture(senderPattern, compact, "حساب المرسل"));
        if (sender.length() < 6 || sender.length() > 20) {
            throw new ParseFailure("حساب المرسل غير صالح");
        }
        return new Parsed(
                transactionId,
                amount.toPlainString(),
                sender,
                isoUtc(occurredAtMillis)
        );
    }

    private static String isoUtc(long occurredAtMillis) {
        SimpleDateFormat formatter = new SimpleDateFormat(
                "yyyy-MM-dd'T'HH:mm:ss.SSS'Z'",
                Locale.US
        );
        formatter.setTimeZone(TimeZone.getTimeZone("UTC"));
        return formatter.format(new Date(occurredAtMillis));
    }

    private static String capture(String expression, String raw, String label)
            throws ParseFailure {
        try {
            Matcher matcher = Pattern.compile(expression).matcher(raw);
            if (!matcher.find() || matcher.groupCount() < 1 || matcher.group(1) == null) {
                throw new ParseFailure("لم يتم العثور على " + label);
            }
            return matcher.group(1);
        } catch (java.util.regex.PatternSyntaxException error) {
            throw new ParseFailure("صيغة مطابقة " + label + " غير صحيحة");
        }
    }

    private static String onlyDigits(String value) {
        return toWesternDigits(value).replaceAll("[^0-9]", "");
    }

    static String toWesternDigits(String value) {
        StringBuilder result = new StringBuilder(value.length());
        for (int index = 0; index < value.length(); index++) {
            char character = value.charAt(index);
            if (character >= '٠' && character <= '٩') {
                result.append((char) ('0' + character - '٠'));
            } else if (character >= '۰' && character <= '۹') {
                result.append((char) ('0' + character - '۰'));
            } else {
                result.append(character);
            }
        }
        return result.toString();
    }

    static final class Parsed {
        final String transactionId;
        final String amount;
        final String sender;
        final String occurredAt;

        Parsed(String transactionId, String amount, String sender, String occurredAt) {
            this.transactionId = transactionId;
            this.amount = amount;
            this.sender = sender;
            this.occurredAt = occurredAt;
        }

        String payload() throws Exception {
            return new JSONObject()
                    .put("transaction_id", transactionId)
                    .put("amount", amount)
                    .put("currency", "YER")
                    .put("sender_account", sender)
                    .put("occurred_at", occurredAt)
                    .toString();
        }
    }

    static final class ParseFailure extends Exception {
        ParseFailure(String message) {
            super(message);
        }
    }
}
