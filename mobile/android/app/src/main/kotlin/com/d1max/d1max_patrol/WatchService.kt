package com.d1max.d1max_patrol

// 后台值守（W17，决策 30）：app 退到后台、锁屏、被划掉之后，照样连着站点的事件流，P1 告警弹系统通知；
// 到了「响铃」那一档（入侵一来就是，决策 29），用闹钟声一直响到有人点开。
//
// 为什么是原生服务、不是 Flutter 那边的连接：app 被划掉时 Flutter 引擎跟着没了，后台值守要的正是那个
// 时候还在。Android 要求后台长连接是前台服务，通知栏常驻一条「值守中」。
//
// 凭据是站点发的**值守令牌**：只能看告警（事件流、告警名单），30 天到期，注销即作废。存在 app 私有的
// SharedPreferences 里（服务被系统杀掉重启时要读回来）。证书照手机 app 一样钉指纹（SHA-256，DER）：
// 系统信不信这张证书都不算数。
//
// 断了要让人知道：连不上超过 [LOST_ALERT_MS] 就弹一条「值守断了」；令牌失效（401/403）弹「值守停了，
// 打开 app 重新登录」并停服务。最坏的情况不是响错，是该响的时候一声不吭。

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.media.AudioAttributes
import android.media.RingtoneManager
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import android.util.Log
import org.json.JSONArray
import org.json.JSONObject
import java.io.BufferedReader
import java.io.InputStreamReader
import java.net.HttpURLConnection
import java.net.URL
import java.security.MessageDigest
import java.security.cert.CertificateException
import java.security.cert.X509Certificate
import javax.net.ssl.HttpsURLConnection
import javax.net.ssl.SSLContext
import javax.net.ssl.TrustManager
import javax.net.ssl.X509TrustManager

class WatchService : Service() {
    companion object {
        const val ACTION_START = "com.d1max.watch.START"
        const val ACTION_STOP = "com.d1max.watch.STOP"
        const val PREFS = "d1max_watch"
        private const val TAG = "D1MaxWatch"
        private const val CH_ONGOING = "watch_ongoing"
        private const val CH_P1 = "watch_p1"
        private const val CH_ALARM = "watch_alarm"
        private const val CH_STATE = "watch_state"
        private const val ID_ONGOING = 1
        private const val ID_STATE = 2
        /** 连不上多久弹「值守断了」。站点每 15 s 发心跳，读超时 45 s。 */
        private const val LOST_ALERT_MS = 2 * 60_000L
        private const val READ_TIMEOUT_MS = 45_000
        private const val RETRY_MS = 5_000L

        @Volatile
        var running = false
            private set
    }

    @Volatile
    private var stopping = false
    private var worker: Thread? = null
    private var wake: PowerManager.WakeLock? = null
    private var site = ""
    private var url = ""
    private var fingerprint = ""
    private var token = ""

    /** 每条告警已经弹到第几档（同一档不重复弹；升档再弹）。 */
    private val shown = HashMap<String, Int>()

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        channels()
        val prefs = getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        when (intent?.action) {
            ACTION_STOP -> {
                // 用 startForegroundService 送进来的:先 startForeground,不然系统按超时崩掉
                foreground("停止中")
                stopWatch(revoke = true)
                return START_NOT_STICKY
            }
            ACTION_START -> prefs.edit()
                .putString("site", intent.getStringExtra("site") ?: "")
                .putString("url", intent.getStringExtra("url") ?: "")
                .putString("fingerprint", intent.getStringExtra("fingerprint") ?: "")
                .putString("token", intent.getStringExtra("token") ?: "")
                .apply()
            // null:系统杀掉之后按 START_STICKY 重启，照存着的接着值守
        }
        site = prefs.getString("site", "") ?: ""
        url = (prefs.getString("url", "") ?: "").trimEnd('/')
        fingerprint = prefs.getString("fingerprint", "") ?: ""
        token = prefs.getString("token", "") ?: ""
        if (url.isEmpty() || token.isEmpty()) {
            stopWatch(revoke = false)
            return START_NOT_STICKY
        }
        foreground("连站点中…")
        if (worker?.isAlive != true) {
            stopping = false
            running = true
            val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
            wake = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "d1max:watch").also {
                it.setReferenceCounted(false)
                it.acquire()
            }
            worker = Thread({ loop() }, "d1max-watch").also { it.start() }
        }
        return START_STICKY
    }

    override fun onDestroy() {
        stopping = true
        running = false
        worker?.interrupt()
        wake?.let { if (it.isHeld) it.release() }
        super.onDestroy()
    }

    private fun stopWatch(revoke: Boolean) {
        stopping = true
        running = false
        val prefs = getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        val oldUrl = (prefs.getString("url", "") ?: "").trimEnd('/')
        val oldToken = prefs.getString("token", "") ?: ""
        val oldFp = prefs.getString("fingerprint", "") ?: ""
        prefs.edit().clear().apply()
        if (revoke && oldUrl.isNotEmpty() && oldToken.isNotEmpty()) {
            // 注销值守令牌（站点上当场作废）。连不上就算了：30 天后自己到期。
            Thread {
                try {
                    val c = open("$oldUrl/api/logout", oldToken, oldFp)
                    c.requestMethod = "POST"
                    c.doOutput = true
                    c.outputStream.use { it.write("{}".toByteArray()) }
                    c.responseCode
                    c.disconnect()
                } catch (e: Exception) {
                    Log.w(TAG, "注销值守令牌没成", e)
                }
            }.start()
        }
        worker?.interrupt()
        val nm = getSystemService(NotificationManager::class.java)
        synchronized(shown) {
            for (k in shown.keys) nm.cancel(k.hashCode())
            shown.clear()
        }
        nm.cancel(ID_STATE)
        if (Build.VERSION.SDK_INT >= 24) stopForeground(STOP_FOREGROUND_REMOVE) else stopForeground(true)
        stopSelf()
    }

    // ------------------------------------------------------------ 连接

    private fun loop() {
        var lostSince = 0L
        var lostTold = false
        while (!stopping) {
            try {
                val c = open("$url/api/events", token, fingerprint)
                c.readTimeout = READ_TIMEOUT_MS
                val code = c.responseCode
                if (code == 401 || code == 403) {
                    c.disconnect()
                    tell(ID_STATE, CH_STATE, "后台值守停了", "值守令牌失效了（注销、改口令、停用或到期）：打开 app 重新登录")
                    stopWatch(revoke = false)
                    return
                }
                if (code != 200) throw java.io.IOException("站点回 $code")
                lostSince = 0L
                if (lostTold) {
                    lostTold = false
                    getSystemService(NotificationManager::class.java).cancel(ID_STATE)
                }
                foreground("连着站点")
                catchUp()
                BufferedReader(InputStreamReader(c.inputStream, Charsets.UTF_8)).use { r ->
                    while (!stopping) {
                        val line = r.readLine() ?: break
                        if (!line.startsWith("data: ")) continue
                        try {
                            frame(JSONObject(line.substring(6)))
                        } catch (e: Exception) {
                            Log.w(TAG, "读不懂的一帧", e)
                        }
                    }
                }
                c.disconnect()
            } catch (e: Exception) {
                if (stopping) return
                Log.w(TAG, "事件流断了", e)
            }
            if (stopping) return
            val now = System.currentTimeMillis()
            if (lostSince == 0L) lostSince = now
            val mins = (now - lostSince) / 60_000
            foreground(if (mins > 0) "连不上站点（已断 $mins 分钟），重试中" else "连不上站点，重试中")
            if (!lostTold && now - lostSince >= LOST_ALERT_MS) {
                lostTold = true
                tell(ID_STATE, CH_STATE, "值守断了", "$site 连不上已经两分钟：这期间的告警手机收不到")
            }
            try {
                Thread.sleep(RETRY_MS)
            } catch (e: InterruptedException) {
                return
            }
        }
    }

    /** 连上（重连上）补一次：没确认的 P1，断线期间升了档的，要补弹、补响。 */
    private fun catchUp() {
        try {
            val c = open("$url/api/alerts", token, fingerprint)
            c.readTimeout = READ_TIMEOUT_MS
            if (c.responseCode != 200) {
                c.disconnect()
                return
            }
            val body = c.inputStream.bufferedReader(Charsets.UTF_8).readText()
            c.disconnect()
            val rows: JSONArray = JSONObject(body).optJSONArray("alerts") ?: return
            for (i in 0 until rows.length()) alert(rows.getJSONObject(i))
        } catch (e: Exception) {
            Log.w(TAG, "补读告警没成", e)
        }
    }

    private fun frame(f: JSONObject) {
        if (f.optString("kind") == "alert") f.optJSONObject("alert")?.let { alert(it) }
    }

    /** 一条告警：P1、没人确认、没解决的弹出来；确认了、解决了的收回去。 */
    private fun alert(a: JSONObject) = synchronized(shown) { alertLocked(a) }

    private fun alertLocked(a: JSONObject) {
        val key = a.optString("key")
        if (key.isEmpty()) return
        val nm = getSystemService(NotificationManager::class.java)
        val done = !a.isNull("acked_ms") || !a.isNull("resolved_ms")
        if (done || a.optString("level") != "P1") {
            if (shown.remove(key) != null) nm.cancel(key.hashCode())
            return
        }
        val tier = a.optInt("escalated", 0)
        val before = shown[key]
        if (before != null && before >= tier) return
        shown[key] = tier
        val title = "${a.optString("level")} ${a.optString("robot")} · ${a.optString("title")}"
        val text = a.optString("detail").ifEmpty { "没人确认：打开 app 处理" }
        val sound = a.optString("channel") == "sound"
        val b = builder(if (sound) CH_ALARM else CH_P1)
            .setContentTitle(title)
            .setContentText(text)
            .setStyle(Notification.BigTextStyle().bigText(text))
            .setSmallIcon(R.mipmap.ic_launcher)
            .setContentIntent(openApp())
            .setAutoCancel(true)
            .setCategory(if (sound) Notification.CATEGORY_ALARM else Notification.CATEGORY_MESSAGE)
        if (Build.VERSION.SDK_INT < 26) {
            b.setPriority(Notification.PRIORITY_MAX)
            if (sound) b.setSound(RingtoneManager.getDefaultUri(RingtoneManager.TYPE_ALARM))
        }
        val n = b.build()
        // 响铃那一档：一直响，直到有人点开、划掉或在 app 里确认（站点推来确认就收回去）
        if (sound) n.flags = n.flags or Notification.FLAG_INSISTENT
        nm.notify(key.hashCode(), n)
    }

    // ------------------------------------------------------------ 钉证书

    private fun open(u: String, tok: String, fp: String): HttpURLConnection {
        val c = URL(u).openConnection() as HttpURLConnection
        if (c is HttpsURLConnection) {
            val ctx = SSLContext.getInstance("TLS")
            ctx.init(null, arrayOf<TrustManager>(Pinned(fp)), null)
            c.sslSocketFactory = ctx.socketFactory
            // 主机名不核：信任只来自指纹（跟手机 app 一样；站点证书的名字是装的时候定的）
            c.hostnameVerifier = javax.net.ssl.HostnameVerifier { _, _ -> true }
        }
        c.connectTimeout = 10_000
        c.instanceFollowRedirects = false
        c.setRequestProperty("Authorization", "Bearer $tok")
        c.setRequestProperty("Accept", "text/event-stream, application/json")
        return c
    }

    private class Pinned(private val fp: String) : X509TrustManager {
        override fun checkClientTrusted(chain: Array<out X509Certificate>?, authType: String?) {
            throw CertificateException("不收客户端证书")
        }

        override fun checkServerTrusted(chain: Array<out X509Certificate>?, authType: String?) {
            val leaf = chain?.firstOrNull() ?: throw CertificateException("没有证书")
            val got = MessageDigest.getInstance("SHA-256").digest(leaf.encoded)
                .joinToString("") { "%02x".format(it) }
            if (got != fp) throw CertificateException("证书指纹对不上")
        }

        override fun getAcceptedIssuers(): Array<X509Certificate> = arrayOf()
    }

    // ------------------------------------------------------------ 通知

    private fun channels() {
        if (Build.VERSION.SDK_INT < 26) return
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(
            NotificationChannel(CH_ONGOING, "值守中", NotificationManager.IMPORTANCE_LOW).apply {
                description = "后台值守常驻的那一条（Android 要求）"
                setShowBadge(false)
            })
        nm.createNotificationChannel(
            NotificationChannel(CH_P1, "P1 告警", NotificationManager.IMPORTANCE_HIGH).apply {
                description = "要人立刻动身的告警"
                enableVibration(true)
            })
        nm.createNotificationChannel(
            NotificationChannel(CH_ALARM, "P1 告警（响铃）", NotificationManager.IMPORTANCE_HIGH).apply {
                description = "入侵、没人确认的 P1：闹钟声一直响到有人处理"
                enableVibration(true)
                setBypassDnd(true)
                setSound(
                    RingtoneManager.getDefaultUri(RingtoneManager.TYPE_ALARM),
                    AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_ALARM)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                        .build())
            })
        nm.createNotificationChannel(
            NotificationChannel(CH_STATE, "值守断了", NotificationManager.IMPORTANCE_HIGH).apply {
                description = "连不上站点、令牌失效：这期间收不到告警"
            })
    }

    private fun builder(ch: String): Notification.Builder =
        if (Build.VERSION.SDK_INT >= 26) Notification.Builder(this, ch)
        else @Suppress("DEPRECATION") Notification.Builder(this)

    private fun openApp(): PendingIntent {
        val i = Intent(this, MainActivity::class.java)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP)
        return PendingIntent.getActivity(this, 0, i, PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
    }

    private fun foreground(state: String) {
        val stopIntent = Intent(this, WatchService::class.java).setAction(ACTION_STOP)
        val flags = PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        val stop = if (Build.VERSION.SDK_INT >= 26) PendingIntent.getForegroundService(this, 1, stopIntent, flags)
        else PendingIntent.getService(this, 1, stopIntent, flags)
        val n = builder(CH_ONGOING)
            .setContentTitle("值守中 · $site")
            .setContentText(state)
            .setSmallIcon(R.mipmap.ic_launcher)
            .setOngoing(true)
            .setContentIntent(openApp())
            .addAction(Notification.Action.Builder(null, "停止值守", stop).build())
            .build()
        if (Build.VERSION.SDK_INT >= 34) {
            startForeground(ID_ONGOING, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
        } else {
            startForeground(ID_ONGOING, n)
        }
    }

    private fun tell(id: Int, ch: String, title: String, text: String) {
        val n = builder(ch)
            .setContentTitle(title)
            .setContentText(text)
            .setStyle(Notification.BigTextStyle().bigText(text))
            .setSmallIcon(R.mipmap.ic_launcher)
            .setContentIntent(openApp())
            .setAutoCancel(true)
            .build()
        getSystemService(NotificationManager::class.java).notify(id, n)
    }
}
