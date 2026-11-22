plugins {
    java
}

repositories {
    mavenCentral()
}

java {
    toolchain.languageVersion.set(JavaLanguageVersion.of(21))
}

val allureVersion = "2.35.1"
val allureDistribution by configurations.creating {
    isCanBeConsumed = false
    isTransitive = false
}

dependencies {
    testImplementation("org.junit.jupiter:junit-jupiter:5.13.4")
    testRuntimeOnly("org.junit.platform:junit-platform-launcher:1.13.4")
    testImplementation("org.seleniumhq.selenium:selenium-java:4.51.0")
    testImplementation("io.qameta.allure:allure-jupiter:2.35.5")
    allureDistribution("io.qameta.allure:allure-commandline:$allureVersion@zip")
}

providers.gradleProperty("qaOutput").orNull?.let {
    layout.buildDirectory.set(layout.projectDirectory.dir(it))
}
val results = layout.buildDirectory.dir("allure-results")
val report = layout.buildDirectory.dir("allure-report")
val allureHome = layout.buildDirectory.dir("allure-cli")
val labAssets = providers.gradleProperty("qaLabAssets")
    .map { file(it) }
    .orElse(layout.buildDirectory.dir("lab-build").map { it.asFile })

val prepareQaReports by tasks.registering(Delete::class) {
    delete(results, report)
}

val extractAllure by tasks.registering(Sync::class) {
    from({ zipTree(allureDistribution.singleFile) })
    into(allureHome)
}

val allureReport by tasks.registering(Exec::class) {
    dependsOn(extractAllure)
    onlyIf {
        results.get().asFile.listFiles()?.any { it.name.endsWith("-result.json") } == true
    }
    doFirst {
        val executable = allureHome.get().file("allure-$allureVersion/bin/allure").asFile
        commandLine("sh", executable, "generate", results.get().asFile, "--clean", "-o", report.get().asFile)
    }
}

val buildLab by tasks.registering(Exec::class) {
    dependsOn(prepareQaReports)
    workingDir(layout.projectDirectory.dir("../../web"))
    commandLine("pnpm", "build:client", "--outDir", labAssets.get().absolutePath)
}

tasks.test {
    dependsOn(buildLab)
    finalizedBy(allureReport)
    val live = providers.gradleProperty("qaLive").orNull
    val newer = providers.gradleProperty("qaLiveNewer").orNull
    useJUnitPlatform {
        if (live == null) excludeTags("live")
        else {
            includeTags(providers.gradleProperty("qaLiveSuite").get())
            if (newer != null) includeTags("d001-archive")
        }
    }
    if (live != null) {
        systemProperty("rag.qa.live.origin", live)
        if (newer != null) systemProperty("rag.qa.live.newer", newer)
        listOf("Article", "Run", "Version", "Publication", "Zip").forEach {
            systemProperty("rag.qa.live." + it.lowercase(), providers.gradleProperty("qaLive" + it).get())
        }
    }
    outputs.upToDateWhen { false }
    systemProperty("rag.lab.assets", labAssets.get().absolutePath)
    systemProperty("allure.results.directory", results.get().asFile.absolutePath)
    systemProperty("rag.qa.failSource", providers.gradleProperty("qaFailSource").orElse("false").get())
    doFirst {
        results.get().asFile.mkdirs()
        val stand = if (live == null && System.getenv("RAG_QA_ORIGIN").isNullOrBlank()) "Local React with synthetic HTTP fixtures"
            else "Local React with live Hono and saved extraction runs"
        results.get().file("environment.properties").asFile.writeText(
            "Stand=$stand\nJava=${JavaVersion.current()}\n" +
                "Revision=${System.getenv("GITHUB_SHA") ?: "local"}\n"
        )
    }
}

tasks.register<Exec>("loadReport") {
    group = "verification"
    description = "Build a separate Allure report from a saved D001 baseline.json"
    dependsOn(extractAllure)
    doFirst {
        val baseline = providers.gradleProperty("loadBaseline").orNull
            ?: throw GradleException("Set -PloadBaseline=/absolute/path/to/baseline.json")
        val loadResults = layout.buildDirectory.dir("load-allure-results").get().asFile
        val loadReport = layout.buildDirectory.dir("load-allure-report").get().asFile
        providers.exec {
            commandLine("python3", layout.projectDirectory.file("load_allure.py").asFile, file(baseline), loadResults)
        }.result.get().assertNormalExitValue()
        val executable = allureHome.get().file("allure-$allureVersion/bin/allure").asFile
        commandLine("sh", executable, "generate", loadResults, "--clean", "-o", loadReport)
    }
}

tasks.register("allurePortable") {
    group = "verification"
    description = "Export the available Allure results as self-contained HTML"
    dependsOn(extractAllure)
    mustRunAfter(tasks.named("loadReport"))
    doLast {
        val executable = allureHome.get().file("allure-$allureVersion/bin/allure").asFile
        val portable = layout.buildDirectory.dir("open-in-browser")
        val inputs = mapOf("browser" to results, "load" to layout.buildDirectory.dir("load-allure-results"))
        for ((name, directory) in inputs) {
            val source = directory.get().asFile
            if (source.listFiles()?.any { it.name.endsWith("-result.json") } != true) continue
            providers.exec {
                commandLine("sh", executable, "generate", source, "--single-file", "--clean", "-o",
                    portable.get().dir(name).asFile)
            }.result.get().assertNormalExitValue()
        }
    }
}

tasks.register<Exec>("readLoadReport") {
    group = "verification"
    description = "Build Allure HTML from a saved D001 read-load.json"
    dependsOn(extractAllure)
    doFirst {
        val input = providers.gradleProperty("qaReadLoad").orNull
            ?: throw GradleException("Set -PqaReadLoad=/absolute/path/to/read-load.json")
        val loadResults = layout.buildDirectory.dir("read-load-allure-results").get().asFile
        val loadReport = layout.buildDirectory.dir("read-load-allure-report").get().asFile
        providers.exec {
            commandLine("python3", layout.projectDirectory.file("load_allure.py").asFile, file(input), loadResults, "--read-load")
        }.result.get().assertNormalExitValue()
        val executable = allureHome.get().file("allure-$allureVersion/bin/allure").asFile
        commandLine("sh", executable, "generate", loadResults, "--single-file", "--clean", "-o", loadReport)
    }
}
