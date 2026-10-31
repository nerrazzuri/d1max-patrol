package com.d1max.d1max_patrol

import android.app.Activity
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.PackageInstaller
import android.net.Uri
import android.os.Build
import android.provider.Settings
import java.io.File

/**
 * W55:从站点下载的新版 App 交给系统安装(Dart 那边已核对大小和指纹)。用系统自带的 [PackageInstaller],
 * 不用 FileProvider:不多依赖、不把文件暴露给别的 App。系统会弹确认框;安卓也会核对新包跟已装的是同一把签名钥匙。
 */
object ApkInstaller {
    /** 回 null = 已交给系统(等人确认);"need_permission" = 先去设置页允许「安装未知应用」。 */
    fun install(act: Activity, path: String): String? {
        if (Build.VERSION.SDK_INT >= 26 && !act.packageManager.canRequestPackageInstalls()) {
            act.startActivity(Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:${act.packageName}")))
            return "need_permission"
        }
        val apk = File(path)
        val installer = act.packageManager.packageInstaller
        val params = PackageInstaller.SessionParams(PackageInstaller.SessionParams.MODE_FULL_INSTALL)
        params.setAppPackageName(act.packageName)          // 只装我们自己的新版,别的包装不进来
        val id = installer.createSession(params)
        installer.openSession(id).use { s ->
            apk.inputStream().use { inp ->
                s.openWrite("update.apk", 0, apk.length()).use { out ->
                    inp.copyTo(out)
                    s.fsync(out)
                }
            }
            val flags = PendingIntent.FLAG_UPDATE_CURRENT or
                (if (Build.VERSION.SDK_INT >= 31) PendingIntent.FLAG_MUTABLE else 0)
            val pi = PendingIntent.getBroadcast(act, id, Intent(act, InstallResultReceiver::class.java), flags)
            s.commit(pi.intentSender)
        }
        return null
    }
}

/** 系统装包的进度回执:要人确认时把确认页拉起来;别的结果不管(装好了 App 会被系统重启)。 */
class InstallResultReceiver : BroadcastReceiver() {
    override fun onReceive(ctx: Context, intent: Intent) {
        if (intent.getIntExtra(PackageInstaller.EXTRA_STATUS, -999) != PackageInstaller.STATUS_PENDING_USER_ACTION) return
        val confirm: Intent? = if (Build.VERSION.SDK_INT >= 33) {
            intent.getParcelableExtra(Intent.EXTRA_INTENT, Intent::class.java)
        } else {
            @Suppress("DEPRECATION") intent.getParcelableExtra(Intent.EXTRA_INTENT)
        }
        confirm?.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)?.let { ctx.startActivity(it) }
    }
}
