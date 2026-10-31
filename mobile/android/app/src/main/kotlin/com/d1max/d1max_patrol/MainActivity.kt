package com.d1max.d1max_patrol

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.PowerManager
import android.provider.Settings
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.MethodChannel

/** 后台值守（W17）的开关在 Dart 那边（`lib/net/background_watch.dart`），这里只转给 [WatchService]。 */
class MainActivity : FlutterActivity() {
    /** 商业化 A6:App 被杀以后点厂商通道的推送通知,拉起的 intent 带着参数,Flutter 3.29 起会当深链接
     *  去找路由、找不到就白屏(极光插件说明)。我们不用深链接:关掉。 */
    override fun shouldHandleDeeplinking(): Boolean = false

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        MethodChannel(flutterEngine.dartExecutor.binaryMessenger, "d1max/watch").setMethodCallHandler { call, result ->
            when (call.method) {
                "start" -> {
                    askOnce()
                    val i = Intent(this, WatchService::class.java).setAction(WatchService.ACTION_START)
                    for (k in listOf("site", "url", "fingerprint", "token")) i.putExtra(k, call.argument<String>(k) ?: "")
                    if (Build.VERSION.SDK_INT >= 26) startForegroundService(i) else startService(i)
                    result.success(true)
                }
                "stop" -> {
                    if (WatchService.running) {
                        val i = Intent(this, WatchService::class.java).setAction(WatchService.ACTION_STOP)
                        if (Build.VERSION.SDK_INT >= 26) startForegroundService(i) else startService(i)
                    }
                    result.success(true)
                }
                "running" -> result.success(WatchService.running)
                else -> result.notImplemented()
            }
        }
        // W42:连机器人的热点(添加机器人、换 WiFi、工程师解绑)
        MethodChannel(flutterEngine.dartExecutor.binaryMessenger, "d1max/hotspot").setMethodCallHandler { call, result ->
            when (call.method) {
                "join" -> RobotHotspot.join(applicationContext, call.argument<String>("ssid") ?: "",
                    call.argument<String>("psk") ?: "") { ok -> result.success(ok) }
                "leave" -> {
                    RobotHotspot.leave(applicationContext)
                    result.success(null)
                }
                else -> result.notImplemented()
            }
        }
        // W55:从站点下载的新版 App 交给系统安装
        MethodChannel(flutterEngine.dartExecutor.binaryMessenger, "d1max/install").setMethodCallHandler { call, result ->
            when (call.method) {
                "apk" -> {
                    val path = call.argument<String>("path") ?: ""
                    Thread {                                  // 几十 MB 写进系统:别卡住界面
                        try {
                            val r = ApkInstaller.install(this, path)
                            runOnUiThread { result.success(r) }
                        } catch (e: Exception) {
                            runOnUiThread { result.error("install_failed", e.message, null) }
                        }
                    }.start()
                }
                else -> result.notImplemented()
            }
        }
        // RC 加固 H9:FCM 的推送号。Firebase 的参数由 Dart 那边带过来(打包时 --dart-define 给的)。
        MethodChannel(flutterEngine.dartExecutor.binaryMessenger, "d1max/push").setMethodCallHandler { call, result ->
            when (call.method) {
                "fcmToken" -> FcmToken.get(
                    applicationContext,
                    call.argument<String>("appId") ?: "", call.argument<String>("apiKey") ?: "",
                    call.argument<String>("projectId") ?: "", call.argument<String>("senderId") ?: "",
                ) { token -> runOnUiThread { result.success(token) } }
                else -> result.notImplemented()
            }
        }
    }

    /** 弹通知要权限（Android 13+）；后台常驻最好不被省电杀掉：都只在开值守时问，拒了不再缠着问。 */
    private fun askOnce() {
        if (Build.VERSION.SDK_INT >= 33 &&
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 17)
        }
        val prefs = getSharedPreferences("d1max_ui", MODE_PRIVATE)
        val pm = getSystemService(PowerManager::class.java)
        if (Build.VERSION.SDK_INT >= 23 && !pm.isIgnoringBatteryOptimizations(packageName) &&
            !prefs.getBoolean("asked_battery", false)) {
            prefs.edit().putBoolean("asked_battery", true).apply()
            try {
                startActivity(Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS)
                    .setData(Uri.parse("package:$packageName")))
            } catch (e: Exception) {
                // 有的系统没有这个页面:算了,文档里写着让值守的人手动设「不限制」
            }
        }
    }
}
