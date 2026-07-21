package com.madhmoun.jeebrelay;

import org.json.JSONObject;

final class RelayEvent {
    final String nonce;
    final String payload;
    final long queuedAt;

    RelayEvent(String nonce, String payload, long queuedAt) {
        this.nonce = nonce;
        this.payload = payload;
        this.queuedAt = queuedAt;
    }

    JSONObject toJson() throws Exception {
        return new JSONObject()
                .put("nonce", nonce)
                .put("payload", payload)
                .put("queued_at", queuedAt);
    }

    static RelayEvent fromJson(JSONObject value) throws Exception {
        return new RelayEvent(
                value.getString("nonce"),
                value.getString("payload"),
                value.getLong("queued_at")
        );
    }
}
