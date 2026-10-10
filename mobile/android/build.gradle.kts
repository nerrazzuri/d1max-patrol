allprojects {
    repositories {
        google()
        mavenCentral()
        // A6 外审 F1:华为、荣耀厂商通道的 SDK 在它们自己的仓库(只有接了才会去拉)
        maven { url = uri("https://developer.huawei.com/repo/") }
        maven { url = uri("https://developer.hihonor.com/repo") }
    }
}

val newBuildDir: Directory =
    rootProject.layout.buildDirectory
        .dir("../../build")
        .get()
rootProject.layout.buildDirectory.value(newBuildDir)

subprojects {
    val newSubprojectBuildDir: Directory = newBuildDir.dir(project.name)
    project.layout.buildDirectory.value(newSubprojectBuildDir)
}
subprojects {
    project.evaluationDependsOn(":app")
}

tasks.register<Delete>("clean") {
    delete(rootProject.layout.buildDirectory)
}
