import java.util.Properties

// 商业化 A6 外审 F1:华为厂商通道要华为的构建插件(agcp)处理 agconnect-services.json。只有把华为开发者
// 平台下载的 agconnect-services.json 放到 android/app/ 下才接(没放就跟原来一样,不碰华为的仓库)。
val huaweiPush = file("agconnect-services.json").exists()
buildscript {
    if (File(projectDir, "agconnect-services.json").exists()) {
        repositories { maven { url = uri("https://developer.huawei.com/repo/") } }
        dependencies { classpath("com.huawei.agconnect:agcp:1.9.1.301") }
    }
}

plugins {
    id("com.android.application")
    // The Flutter Gradle Plugin must be applied after the Android and Kotlin Gradle plugins.
    id("dev.flutter.flutter-gradle-plugin")
}

// 正式签名(W30,决策 43):android/key.properties(不进仓库)写着 keystore 在哪、口令。
// 有它就用正式钥匙签 release;没有就退回调试钥匙(CI、开发机能编)—— 发给客户的包要加
// -Pd1max.requireReleaseKey=true,没有正式钥匙就直接失败,不会悄悄发出调试签名的包。
val keyProps = Properties().apply {
    val f = rootProject.file("key.properties")
    if (f.exists()) f.inputStream().use { load(it) }
}
val haveReleaseKey = keyProps.getProperty("storeFile") != null
if (!haveReleaseKey && (project.findProperty("d1max.requireReleaseKey") as String?) == "true") {
    throw GradleException("没有 android/key.properties:发给客户的包必须用正式钥匙签(见 docs/W30-完工报告.md)")
}

// ------------------------------------------------------------ A6 外审 F1:极光厂商通道
// 首批支持:小米、华为、荣耀、OPPO、vivo。每家「给了参数才接」:
//   -PjpushXiaomiAppId=… -PjpushXiaomiAppKey=…
//   -PjpushOppoAppKey=… -PjpushOppoAppId=… -PjpushOppoAppSecret=…
//   -PjpushVivoAppKey=… -PjpushVivoAppId=…
//   -PjpushHonorAppId=…
//   华为:android/app/agconnect-services.json(华为开发者平台下载)
// 参数格式照极光说明:小米的值前面加 "MI-"、OPPO 加 "OP-"(这里自动加)。
fun vendorProp(p: String): String = (project.findProperty(p) as String?) ?: ""
data class PushVendor(val name: String, val placeholders: Map<String, String>, val prefix: String,
                      val deps: List<String>, val enabled: () -> Boolean)
val jiguangPlugin = "6.2.1"
val pushVendors = listOf(
    PushVendor("xiaomi", mapOf("XIAOMI_APPID" to "jpushXiaomiAppId", "XIAOMI_APPKEY" to "jpushXiaomiAppKey"),
               "MI-", listOf("cn.jiguang.sdk.plugin:xiaomi:$jiguangPlugin")) {
        vendorProp("jpushXiaomiAppId").isNotEmpty() },
    PushVendor("oppo", mapOf("OPPO_APPKEY" to "jpushOppoAppKey", "OPPO_APPID" to "jpushOppoAppId",
                             "OPPO_APPSECRET" to "jpushOppoAppSecret"),
               "OP-", listOf("cn.jiguang.sdk.plugin:oppo:$jiguangPlugin")) {
        vendorProp("jpushOppoAppKey").isNotEmpty() },
    PushVendor("vivo", mapOf("VIVO_APPKEY" to "jpushVivoAppKey", "VIVO_APPID" to "jpushVivoAppId"),
               "", listOf("cn.jiguang.sdk.plugin:vivo:$jiguangPlugin")) {
        vendorProp("jpushVivoAppKey").isNotEmpty() },
    PushVendor("honor", mapOf("HONOR_APPID" to "jpushHonorAppId"),
               "", listOf("cn.jiguang.sdk.plugin:honor:$jiguangPlugin")) {
        vendorProp("jpushHonorAppId").isNotEmpty() },
    PushVendor("huawei", emptyMap(), "",
               listOf("cn.jiguang.sdk.plugin:huawei:$jiguangPlugin", "com.huawei.hms:push:6.12.0.300")) {
        huaweiPush },
)

android {
    namespace = "com.d1max.d1max_patrol"
    compileSdk = flutter.compileSdkVersion
    ndkVersion = flutter.ndkVersion

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    defaultConfig {
        // TODO: Specify your own unique Application ID (https://developer.android.com/studio/build/application-id.html).
        applicationId = "com.d1max.d1max_patrol"
        // You can update the following values to match your application needs.
        // For more information, see: https://flutter.dev/to/review-gradle-config.
        minSdk = flutter.minSdkVersion
        targetSdk = flutter.targetSdkVersion
        // Uses the version code from pubspec.yaml. When using split APKs, 1000 * ABI_VERSION
        // is added automatically by Flutter. (https://developer.android.com/studio/build/configure-apk-splits#configure-APK-versions)
        // You can force using the value of versionCode by specifying the `-P force-version-code-ignoring-abi=true`
        // flag during build.
        versionCode = flutter.versionCode
        versionName = flutter.versionName
        // 商业化 A6:极光推送。AppKey 打包时给(-PjpushAppKey=… 或 ~/.gradle/gradle.properties),不进仓库;
        // 没给就是空的:App 照常用,只是收不到极光推送(后台值守照旧)。
        manifestPlaceholders += mapOf(
            "JPUSH_PKGNAME" to "com.d1max.d1max_patrol",
            "JPUSH_APPKEY" to (project.findProperty("jpushAppKey") as String? ?: ""),
            "JPUSH_CHANNEL" to "developer-default",
        )
        // A6 外审 F1:厂商通道的参数(App 被清掉以后靠它们送达)。各厂商开发者平台拿到,打包时给
        // (-P…,不进仓库)。给了哪家才接哪家的插件(见文末 dependencies);没给的占位是空串。
        manifestPlaceholders += pushVendors.flatMap { v ->
            v.placeholders.map { (k, p) -> k to vendorProp(p).let { if (it.isEmpty()) it else v.prefix + it } }
        }

    }

    signingConfigs {
        if (haveReleaseKey) {
            create("release") {
                storeFile = file(keyProps.getProperty("storeFile"))
                storePassword = keyProps.getProperty("storePassword")
                keyAlias = keyProps.getProperty("keyAlias")
                keyPassword = keyProps.getProperty("keyPassword")
            }
        }
    }

    buildTypes {
        release {
            signingConfig = if (haveReleaseKey) signingConfigs.getByName("release")
                            else signingConfigs.getByName("debug")
        }
    }
}

kotlin {
    compilerOptions {
        jvmTarget = org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17
    }
}

flutter {
    source = "../.."
}

dependencies {
    // 后台值守的纯判断(AlertBell)用 JUnit 测:cd android && ./gradlew :app:testReleaseUnitTest(W17 外审)
    testImplementation("junit:junit:4.13.2")
    // A6 外审 F1:给了参数的厂商才接它的极光厂商插件(版本跟 jpush_flutter 带的极光 SDK 6.2.1 一致)
    for (v in pushVendors.filter { it.enabled() }) {
        for (d in v.deps) implementation(d)
    }
}

if (huaweiPush) {
    apply(plugin = "com.huawei.agconnect")
}
