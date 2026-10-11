package com.d1max.d1max_patrol

import android.content.Context
import com.google.firebase.FirebaseApp
import com.google.firebase.FirebaseOptions
import com.google.firebase.messaging.FirebaseMessaging

/**
 * FCM 的推送号(RC 加固 H9,决策 56)。带 Google 服务的手机走 FCM,跟极光并列;站点对同一部手机只走一个。
 *
 * 不用 google-services 的 Gradle 插件、不放 google-services.json:四个参数打包时经 Dart 给进来
 * (`--dart-define=FCM_…`),这里自己起 Firebase。没给参数这个类不会被调到。
 * 拿不到(没有 Google 服务、参数不对、没网)一律回 null:那台手机只走极光,不报错、不崩。
 * 通知由 Firebase 自己在 App 不在前台时弹(站点发的是带 notification 的消息),这里不用写接收的代码。
 */
object FcmToken {
    fun get(ctx: Context, appId: String, apiKey: String, projectId: String, senderId: String,
            done: (String?) -> Unit) {
        if (appId.isEmpty() || apiKey.isEmpty() || projectId.isEmpty() || senderId.isEmpty()) return done(null)
        try {
            if (FirebaseApp.getApps(ctx).isEmpty()) {
                FirebaseApp.initializeApp(ctx, FirebaseOptions.Builder()
                    .setApplicationId(appId).setApiKey(apiKey).setProjectId(projectId)
                    .setGcmSenderId(senderId).build())
            }
            FirebaseMessaging.getInstance().token.addOnCompleteListener { t ->
                done(if (t.isSuccessful) t.result else null)
            }
        } catch (e: Exception) {
            done(null)
        }
    }
}
