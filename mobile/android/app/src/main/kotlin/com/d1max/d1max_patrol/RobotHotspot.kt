package com.d1max.d1max_patrol

import android.content.Context
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import android.net.wifi.WifiNetworkSpecifier
import android.os.Build
import android.os.Handler
import android.os.Looper

/**
 * 连机器人的热点（W42 §9，Dart 那边是 `lib/net/robot_bind.dart`）。Android 10 起用系统的「指定网络」：
 * 系统弹一个框让人点连接；连上以后**只把这个 App 绑到热点上**（热点没外网，手机别的照常走移动数据）。
 * 用完 [leave] 解绑。老 Android 回 false，界面上教人去系统设置里手动连。
 */
object RobotHotspot {
    private var callback: ConnectivityManager.NetworkCallback? = null

    fun join(ctx: Context, ssid: String, psk: String, done: (Boolean) -> Unit) {
        if (Build.VERSION.SDK_INT < 29) {
            done(false)
            return
        }
        val cm = ctx.getSystemService(ConnectivityManager::class.java)
        leave(ctx)
        val spec = WifiNetworkSpecifier.Builder().setSsid(ssid).apply {
            if (psk.isNotEmpty()) setWpa2Passphrase(psk)
        }.build()
        val req = NetworkRequest.Builder()
            .addTransportType(NetworkCapabilities.TRANSPORT_WIFI)
            .removeCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
            .setNetworkSpecifier(spec)
            .build()
        var answered = false
        val main = Handler(Looper.getMainLooper())
        fun answer(ok: Boolean) = main.post {
            if (!answered) {
                answered = true
                done(ok)
            }
        }
        val cb = object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(network: Network) {
                cm.bindProcessToNetwork(network)
                answer(true)
            }

            override fun onUnavailable() = answer(false)

            override fun onLost(network: Network) {
                cm.bindProcessToNetwork(null)
            }
        }
        callback = cb
        cm.requestNetwork(req, cb, 60_000)
    }

    fun leave(ctx: Context) {
        val cm = ctx.getSystemService(ConnectivityManager::class.java)
        cm.bindProcessToNetwork(null)
        callback?.let {
            try {
                cm.unregisterNetworkCallback(it)
            } catch (e: IllegalArgumentException) {
                // 已经注销过了
            }
        }
        callback = null
    }
}
