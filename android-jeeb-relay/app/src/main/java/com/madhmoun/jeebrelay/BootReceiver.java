package com.madhmoun.jeebrelay;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

public final class BootReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context context, Intent intent) {
        if (PendingQueue.size(context) > 0) {
            RelayJobService.schedule(context);
        }
    }
}
