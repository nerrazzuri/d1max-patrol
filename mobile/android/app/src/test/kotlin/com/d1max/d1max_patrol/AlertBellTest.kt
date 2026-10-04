package com.d1max.d1max_patrol

import org.junit.Assert.assertEquals
import org.junit.Test

// 后台值守「该弹、该收、该重弹」（W17 外审）：只有站点说确认了、解决了才收；响铃档被划掉要再弹。
class AlertBellTest {
    private fun ring(key: String = "A/intrusion#1", done: Boolean = false, tier: Int = 2) =
        AlertFacts(key = key, p1 = true, done = done, tier = tier, sound = tier == 2)

    @Test
    fun 响铃档_弹一次_同一档的帧不重弹() {
        val b = AlertBell()
        assertEquals(BellAction.Post("A/intrusion#1", true), b.onAlert(ring()))
        assertEquals(BellAction.None, b.onAlert(ring()))
    }

    @Test
    fun 被划掉了_还没确认_再弹接着响_确认了才收() {
        val b = AlertBell()
        b.onAlert(ring())
        assertEquals(BellAction.Post("A/intrusion#1", true), b.onDismissed("A/intrusion#1"))
        assertEquals(BellAction.Post("A/intrusion#1", true), b.onDismissed("A/intrusion#1"))
        assertEquals("重弹之后同一档的帧不再叠一条", BellAction.None, b.onAlert(ring()))
        assertEquals(BellAction.Cancel("A/intrusion#1"), b.onAlert(ring(done = true)))
        assertEquals("确认了:划掉就是划掉", BellAction.None, b.onDismissed("A/intrusion#1"))
    }

    @Test
    fun 重连的首帧_没确认的照样在_不会因为弹过就永远不响() {
        val b = AlertBell()
        b.onAlert(ring())
        b.onDismissed("A/intrusion#1")                 // 重弹了
        assertEquals(emptyList<BellAction>(), b.onSnapshot(listOf(ring())))
        // 外审的路径:被划掉 → 先记成没弹 → 首帧里还挂着就补弹
        val b2 = AlertBell()
        b2.onAlert(AlertFacts("A/stuck#1", p1 = true, done = false, tier = 0, sound = false))
        assertEquals("屏幕档:划掉随它", BellAction.None, b2.onDismissed("A/stuck#1"))
        assertEquals(listOf(BellAction.Post("A/stuck#1", false)),
            b2.onSnapshot(listOf(AlertFacts("A/stuck#1", p1 = true, done = false, tier = 0, sound = false))))
    }

    @Test
    fun 升档再弹_首帧里没了的收掉_确认了的收掉() {
        val b = AlertBell()
        val stuck = AlertFacts("A/stuck#1", p1 = true, done = false, tier = 0, sound = false)
        assertEquals(BellAction.Post("A/stuck#1", false), b.onAlert(stuck))
        assertEquals(BellAction.Post("A/stuck#1", true), b.onAlert(stuck.copy(tier = 2, sound = true)))
        b.onAlert(ring("B/fallen#1"))
        val acts = b.onSnapshot(listOf(ring("B/fallen#1", done = true)))
        assertEquals(setOf(BellAction.Cancel("A/stuck#1"), BellAction.Cancel("B/fallen#1")), acts.toSet())
        assertEquals(BellAction.None, b.onDismissed("A/stuck#1"))
    }

    @Test
    fun 不是P1的不弹_停值守全收() {
        val b = AlertBell()
        assertEquals(BellAction.None, b.onAlert(AlertFacts("A/finding#1", p1 = false, done = false, tier = 0, sound = false)))
        b.onAlert(ring())
        assertEquals(listOf("A/intrusion#1"), b.clear())
        assertEquals(BellAction.None, b.onDismissed("A/intrusion#1"))
    }
}
