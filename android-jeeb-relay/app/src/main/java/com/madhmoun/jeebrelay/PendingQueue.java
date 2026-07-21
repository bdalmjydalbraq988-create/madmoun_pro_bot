package com.madhmoun.jeebrelay;

import android.content.Context;
import android.content.SharedPreferences;

import org.json.JSONArray;

import java.util.ArrayList;
import java.util.List;

final class PendingQueue {
    private static final String KEY = "pending_events_v1";
    private static final int MAX_EVENTS = 100;

    private PendingQueue() {}

    static synchronized boolean add(Context context, RelayEvent event) {
        try {
            List<RelayEvent> events = read(context);
            for (RelayEvent existing : events) {
                if (existing.nonce.equals(event.nonce)) {
                    return false;
                }
            }
            events.add(event);
            while (events.size() > MAX_EVENTS) {
                events.remove(0);
            }
            write(context, events);
            return true;
        } catch (Exception error) {
            RelayConfig.status(context, "تعذر حفظ إشعار جيب محليًا: " + error.getMessage());
            return false;
        }
    }

    static synchronized List<RelayEvent> read(Context context) {
        List<RelayEvent> result = new ArrayList<>();
        String raw = RelayConfig.prefs(context).getString(KEY, "[]");
        try {
            JSONArray array = new JSONArray(raw);
            for (int index = 0; index < array.length(); index++) {
                result.add(RelayEvent.fromJson(array.getJSONObject(index)));
            }
        } catch (Exception error) {
            RelayConfig.status(context, "قائمة الإرسال تالفة وتم إيقافها للمراجعة");
        }
        return result;
    }

    static synchronized void remove(Context context, String nonce) {
        List<RelayEvent> events = read(context);
        events.removeIf(item -> item.nonce.equals(nonce));
        write(context, events);
    }

    static synchronized int size(Context context) {
        return read(context).size();
    }

    private static void write(Context context, List<RelayEvent> events) {
        JSONArray array = new JSONArray();
        try {
            for (RelayEvent event : events) {
                array.put(event.toJson());
            }
        } catch (Exception error) {
            throw new IllegalStateException(error);
        }
        SharedPreferences.Editor editor = RelayConfig.prefs(context).edit();
        editor.putString(KEY, array.toString());
        if (!editor.commit()) {
            throw new IllegalStateException("SharedPreferences commit failed");
        }
    }
}
