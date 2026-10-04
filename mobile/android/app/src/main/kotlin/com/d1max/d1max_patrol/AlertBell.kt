package com.d1max.d1max_patrol

// 后台值守「该弹、该收、该重弹」的判断（W17 外审）：不碰 Android，JUnit 直接测（src/test/）。
//
// 规矩：**只有站点说确认了、解决了才收**。点开通知只是打开 app，不算确认；响铃那一档被划掉（Android 14
// 起常驻通知也划得掉）而告警还没确认，就再弹、接着响。外审抓的：原先弹之前就记「这一档弹过了」，通知一被
// 划掉或点掉，之后同一档的帧、重连补读都跳过 —— 告警还没人确认，手机却再也不响。

/** 一条告警里判断要用的几样（从站点的告警报文里取）。 */
data class AlertFacts(
    val key: String,
    val p1: Boolean,
    /** 有人确认了，或解决了。 */
    val done: Boolean,
    val tier: Int,
    /** 到了响铃那一档（`channel == "sound"`）。 */
    val sound: Boolean,
)

sealed class BellAction {
    object None : BellAction()
    /** 弹（或重弹）这一条；[sound] 为真用闹钟声一直响、划不掉、点开也不收。 */
    data class Post(val key: String, val sound: Boolean) : BellAction()
    data class Cancel(val key: String) : BellAction()
}

class AlertBell {
    /** 弹出去的、还挂着的：键 → 弹到第几档。 */
    private val shown = HashMap<String, Int>()
    /** 还没确认、没解决的 P1：键 → 最近一次的事实。重弹用。 */
    private val active = HashMap<String, AlertFacts>()

    @Synchronized
    fun onAlert(a: AlertFacts): BellAction {
        if (a.key.isEmpty()) return BellAction.None
        if (!a.p1 || a.done) {
            active.remove(a.key)
            return if (shown.remove(a.key) != null) BellAction.Cancel(a.key) else BellAction.None
        }
        active[a.key] = a
        val before = shown[a.key]
        if (before != null && before >= a.tier) return BellAction.None
        shown[a.key] = a.tier
        return BellAction.Post(a.key, a.sound)
    }

    /**
     * 连上（重连上）的首帧：站点眼下没解决的告警全量。不在里面的（断线期间解决了的）收掉；在里面的照
     * [onAlert] 走（断线期间升了档的补弹、确认了的收）。
     */
    @Synchronized
    fun onSnapshot(alerts: List<AlertFacts>): List<BellAction> {
        val keys = alerts.map { it.key }.toSet()
        val out = ArrayList<BellAction>()
        for (k in shown.keys.toList()) {
            if (k !in keys) {
                shown.remove(k)
                active.remove(k)
                out.add(BellAction.Cancel(k))
            }
        }
        for (k in active.keys.toList()) if (k !in keys) active.remove(k)
        for (a in alerts) {
            val act = onAlert(a)
            if (act != BellAction.None) out.add(act)
        }
        return out
    }

    /**
     * 通知被划掉了（或被系统收了）。告警还挂着、在响铃那一档：再弹，接着响。别的档：随它（不记「弹过了」，
     * 之后升档、重连的首帧会再弹）。确认过、解决过的：什么都不做。
     */
    @Synchronized
    fun onDismissed(key: String): BellAction {
        shown.remove(key)
        val a = active[key] ?: return BellAction.None
        if (!a.sound) return BellAction.None
        shown[key] = a.tier
        return BellAction.Post(key, true)
    }

    /** 停值守：挂着的全收。回要收的键。 */
    @Synchronized
    fun clear(): List<String> {
        val keys = shown.keys.toList()
        shown.clear()
        active.clear()
        return keys
    }
}
