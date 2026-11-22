package rag.qa;

import io.qameta.allure.Allure;
import io.qameta.allure.Epic;
import io.qameta.allure.Feature;
import io.qameta.allure.Story;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;
import org.openqa.selenium.By;
import org.openqa.selenium.OutputType;
import org.openqa.selenium.WebElement;
import org.openqa.selenium.chrome.ChromeDriver;
import org.openqa.selenium.chrome.ChromeDriverService;
import org.openqa.selenium.chrome.ChromeOptions;
import org.openqa.selenium.interactions.PointerInput;
import org.openqa.selenium.interactions.Sequence;
import org.openqa.selenium.json.Json;
import org.openqa.selenium.support.ui.ExpectedConditions;
import org.openqa.selenium.support.ui.WebDriverWait;

import java.io.ByteArrayInputStream;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.MessageDigest;
import java.time.Duration;
import java.util.HashMap;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;
import java.util.regex.Pattern;
import java.util.zip.ZipInputStream;

import static org.junit.jupiter.api.Assertions.*;

@Epic("Браузерные проверки")
@EnabledIfEnvironmentVariable(named = "RAG_QA_ORIGIN", matches = ".+")
final class LiveLabAcceptanceTest {
    private final HttpClient http = HttpClient.newBuilder().version(HttpClient.Version.HTTP_1_1)
            .connectTimeout(Duration.ofSeconds(5)).build();
    private final Json json = new Json();
    private ChromeDriver browser;
    private URI origin;

    @BeforeEach
    void openBrowser() {
        origin = URI.create(System.getenv("RAG_QA_ORIGIN"));
        var service = new ChromeDriverService.Builder();
        String driverPath = System.getenv("CHROMEDRIVER");
        if (driverPath != null) service.usingDriverExecutable(Path.of(driverPath).toFile());
        var options = new ChromeOptions().addArguments("--headless=new", "--window-size=1440,1000", "--disable-dev-shm-usage");
        String chromePath = System.getenv("CHROME_BIN");
        if (chromePath != null) options.setBinary(chromePath);
        browser = new ChromeDriver(service.build(), options);
    }

    @AfterEach
    void captureBrowser() {
        if (browser == null) return;
        try {
            screenshot("Состояние рабочей лаборатории");
            Allure.addAttachment("Адрес", browser.getCurrentUrl());
        } finally {
            browser.quit();
        }
    }

    @Test
    @EnabledIfEnvironmentVariable(named = "RAG_QA_D001_RUN_ID", matches = ".+")
    @Feature("Паспорт и выгрузка")
    @Story("Живой стенд")
    @DisplayName("D001 · ячейка .422 и ZIP открываются из одного выпуска")
    void digitalPublication() throws Exception {
        String path = runPath("D001");
        browser.get(origin.resolve(path + "&tab=run").toString());
        click(By.linkText("Открыть результаты"));
        visible(By.cssSelector("input[type=search]")).sendKeys("ThreadRAG LLaMA3-8B-Instruct HotpotQA EM .422");
        By result = By.xpath("//tr[td[4][normalize-space(.)='EM'] and td[6][contains(.,'Table 1') or contains(.,'Таблица 1')]]//button[normalize-space(.)='.422']");
        click(result);
        URI source = assertPinnedPublicationSource("dev/D001.pdf", 6);
        screenshot("D001 · .422 · Table 1 · страница 6");
        browser.navigate().refresh();
        assertEquals(source, assertPinnedPublicationSource("dev/D001.pdf", 6));
        click(button("Паспорт и выгрузка"));
        assertPinnedZip(source, 300);
        click(By.cssSelector("button[aria-label='Закрыть паспорт']"));
        click(By.linkText("Проверки"));
        visible(By.cssSelector("button[data-remark]"));
    }

    @Test
    @EnabledIfEnvironmentVariable(named = "RAG_QA_D013_RUN_ID", matches = ".+")
    @Feature("Источники PDF")
    @Story("Живой стенд")
    @DisplayName("D013 · среднее и SD ведут к одной ячейке оригинального скана")
    void scannedPublication() throws Exception {
        browser.get(origin.resolve(runPath("D013") + "&tab=run").toString());
        click(By.linkText("Открыть результаты"));
        visible(By.cssSelector("input[type=search]")).sendKeys("OAS Qwen2.5-3B-Instruct HotpotQA EM");
        click(button("24.5"));
        URI meanSource = assertPinnedPublicationSource("dev/D013.pdf", 6);
        String meanHighlight = visible(By.cssSelector(".react-pdf-highlight")).getDomAttribute("style");
        screenshot("D013 · mean 24.5 · страница 6");
        click(By.xpath("//button[@data-result and (normalize-space(.)='.29' or normalize-space(.)='0.29')]"));
        assertEquals(meanSource, assertPinnedPublicationSource("dev/D013.pdf", 6));
        assertEquals(meanHighlight, visible(By.cssSelector(".react-pdf-highlight")).getDomAttribute("style"));
        screenshot("D013 · SD 0.29 · страница 6");
        click(button("Паспорт и выгрузка"));
        assertPinnedZip(meanSource, 144);
    }

    @Test
    @EnabledIfEnvironmentVariable(named = "RAG_QA_P079_RUN_ID", matches = ".+")
    @Feature("Проверки и ручные правки")
    @Story("Живой стенд")
    @DisplayName("P079 · проверки и оригинальный PDF доступны из прогона без выпуска")
    void checksWithoutPublication() throws Exception {
        String run = System.getenv("RAG_QA_P079_RUN_ID");
        browser.get(origin.resolve(runPath("P079") + "&tab=run").toString());
        visible(By.xpath("//*[@role='status' and contains(.,'Нет подтверждённых результатов')]"));
        click(By.linkText("Проверки"));
        click(By.xpath("//button[contains(normalize-space(.),'Не удалось однозначно связать число с условиями')]"));
        assertEquals("0.1819", visible(By.cssSelector(".review p")).getText());
        URI source = assertPinnedRunSource(run, "test/P079.pdf", 5);
        screenshot("P079 · проверка открывает исходную ячейку без выпуска");
        browser.navigate().refresh();
        assertEquals(source, assertPinnedRunSource(run, "test/P079.pdf", 5));
        Map<?, ?> card = runCard(run);
        assertEquals("empty", card.get("publicationStatus"));
        assertNull(card.get("publication"));
    }

    @Test
    @EnabledIfEnvironmentVariable(named = "RAG_QA_D001_RUN_ID", matches = ".+")
    @Feature("Библиотека и навигация")
    @Story("Живой стенд")
    @DisplayName("D001 · Библиотека и вкладки остаются одной группой при прокрутке")
    void articleNavigationStaysTogether() {
        browser.get(origin.resolve(runPath("D001") + "&tab=results").toString());
        var library = visible(By.linkText("Библиотека"));
        var tabs = visible(By.cssSelector("nav[aria-label='Статья']"));
        var results = visible(By.cssSelector(".results [data-result]"));
        assertTrue(results.isDisplayed());
        assertEquals(library.getRect().getWidth(), tabs.getRect().getWidth(), 1);
        assertTrue(library.getRect().getY() < tabs.getRect().getY());
        assertNotEquals("0px", library.findElement(By.xpath("..")).getCssValue("border-bottom-width"));
        assertTrue((boolean) browser.executeScript("return arguments[0].parentElement.parentElement === arguments[1].parentElement", library, tabs));

        browser.executeScript("window.scrollTo(0, 600)");
        waitFor().until(driver -> ((Number) browser.executeScript("return window.scrollY")).intValue() >= 350);
        var libraryTop = (Number) browser.executeScript("return arguments[0].getBoundingClientRect().top", library);
        var tabsTop = (Number) browser.executeScript("return arguments[0].getBoundingClientRect().top", tabs);
        assertTrue(libraryTop.doubleValue() >= 0 && libraryTop.doubleValue() < 60);
        assertTrue(tabsTop.doubleValue() > libraryTop.doubleValue() && tabsTop.doubleValue() < 120);
    }

    @Test
    @EnabledIfEnvironmentVariable(named = "RAG_QA_D001_CAPTION_RUN_ID", matches = ".+")
    @Feature("Проверки и ручные правки")
    @Story("Живой стенд")
    @DisplayName("D001 · выделить панель, уточнить рамку и явно применить результат")
    void operatorRegion() throws Exception {
        String parent = System.getenv("RAG_QA_D001_CAPTION_RUN_ID");
        String auto = System.getenv("RAG_QA_D001_RUN_ID");
        var oldPublication = (Map<?, ?>) runCard(auto).get("publication");
        URI oldZip = origin.resolve(oldPublication.get("downloadUrl").toString());
        byte[] oldZipBytes = download(oldZip);
        byte[] oldPdfBytes = download(origin.resolve(oldZip.getPath().replace("export.zip", "source.pdf")));
        String address = "/?lab=1&article=" + System.getenv("RAG_QA_D001_ARTICLE_ID") + "&tab=checks";
        browser.get(origin.resolve(address + "&run=" + parent).toString());

        click(button("no unambiguous baseline for grouped vector bars"));
        assertPinnedRunSource(parent, "dev/D001.pdf", 8);
        assertCaptionHighlight();
        screenshot("D001 · подпись Figure 3 без рамки панели");
        browser.executeScript("window.ragAcceptanceMarker = 'region-tab'");

        click(button("Выделить область"));
        click(button("Вся страница"));
        double[] twoPanels = {.112, .080, .510, .210};
        dragRegion(twoPanels);
        click(button("Разобрать область"));
        var ambiguity = By.xpath("//button[contains(.,'несколько панелей')]");
        visible(ambiguity);
        String broadRun = currentRun();
        assertNotEquals(parent, broadRun);
        assertRegionPlan(broadRun, parent, twoPanels);
        assertNull(runCard(broadRun).get("publication"));
        assertTrue(browser.findElements(button("Применить")).isEmpty());
        click(ambiguity);
        assertPinnedRunSource(broadRun, "dev/D001.pdf", 8);
        screenshot("D001 · две панели, требуется уточнение");

        click(button("Уточнить рамку"));
        click(button("Вся страница"));
        double[] panelA = {.112, .080, .331, .210};
        dragRegion(panelA);
        click(button("Разобрать область"));
        var point = By.xpath("//button[contains(.,'HotpotQA') and contains(.,'EM') and contains(.,'anchors=1')]");
        var value = Pattern.compile("0\\.\\d+").matcher(visible(point).getText());
        assertTrue(value.find(), "Значение EM прочитано");
        assertEquals(0.528, Double.parseDouble(value.group()), 0.001000001);
        assertEquals("region-tab", browser.executeScript("return window.ragAcceptanceMarker"));
        String narrowRun = currentRun();
        assertNotEquals(broadRun, narrowRun);
        assertRegionPlan(narrowRun, broadRun, panelA);
        assertNull(runCard(narrowRun).get("publication"));
        assertArrayEquals(oldZipBytes, download(oldZip));

        click(point);
        assertPinnedRunSource(narrowRun, "dev/D001.pdf", 8);
        screenshot("D001 · подтверждённая точка до применения");
        browser.navigate().refresh();
        assertPinnedRunSource(narrowRun, "dev/D001.pdf", 8);
        click(button("Применить"));
        click(By.linkText("Открыть опубликованную версию"));
        click(By.xpath("//button[@data-result and contains(.,'" + value.group() + "')]"));
        URI newSource = assertPinnedPublicationSource("dev/D001.pdf", 8);
        assertNotEquals(oldZip.getPath().replace("export.zip", "source.pdf"), newSource.getPath());
        click(button("Паспорт и выгрузка"));
        assertPinnedZip(newSource, 7);
        assertArrayEquals(oldZipBytes, download(oldZip));
        assertArrayEquals(oldPdfBytes, download(origin.resolve(oldZip.getPath().replace("export.zip", "source.pdf"))));
        assertNotNull(runCard(broadRun));
    }

    @Test
    @EnabledIfEnvironmentVariable(named = "RAG_QA_D001_FAILED_RUN_ID", matches = ".+")
    @Feature("Загрузка и прогон")
    @Story("Живой стенд")
    @DisplayName("D001 · технический повтор использует сохранённую рамку")
    void technicalRetryFromSavedCrop() throws Exception {
        String failed = System.getenv("RAG_QA_D001_FAILED_RUN_ID");
        String article = System.getenv("RAG_QA_D001_ARTICLE_ID");
        browser.get(origin.resolve("/?lab=1&article=" + article + "&run=" + failed + "&tab=checks").toString());
        visible(By.xpath("//*[@role='status' and contains(.,'Прогон области завершился с ошибкой')]"));
        Map<?, ?> saved = regionTarget(failed);
        assertPinnedRunSource(failed, "dev/D001.pdf", 8);
        screenshot("D001 · технический сбой и сохранённая рамка");
        click(button("Повторить разбор"));
        visible(By.xpath("//button[contains(.,'HotpotQA') and contains(.,'EM') and contains(.,'anchors=1')]"));
        String retried = currentRun();
        assertNotEquals(failed, retried);
        Map<?, ?> repeated = regionTarget(retried);
        for (String field : List.of("pdf_sha256", "page", "box")) {
            assertEquals(saved.get(field), repeated.get(field), field);
        }
        assertNull(runCard(retried).get("publication"));
    }

    private void dragRegion(double[] box) {
        var canvas = visible(By.cssSelector(".react-pdf-frame canvas"));
        browser.executeScript("arguments[0].parentElement.scrollIntoView({ block: 'start' });", canvas);
        List<?> edges = (List<?>) browser.executeScript("const r=arguments[0].getBoundingClientRect(); return [r.left,r.top,r.width,r.height];", canvas);
        double left = ((Number) edges.get(0)).doubleValue();
        double top = ((Number) edges.get(1)).doubleValue();
        double width = ((Number) edges.get(2)).doubleValue();
        double height = ((Number) edges.get(3)).doubleValue();
        var pointer = new PointerInput(PointerInput.Kind.MOUSE, "operator");
        var drag = new Sequence(pointer, 0);
        var viewport = PointerInput.Origin.viewport();
        drag.addAction(pointer.createPointerMove(Duration.ZERO, viewport, (int) Math.round(left + width * box[0]), (int) Math.round(top + height * box[1])));
        drag.addAction(pointer.createPointerDown(PointerInput.MouseButton.LEFT.asArg()));
        drag.addAction(pointer.createPointerMove(Duration.ofMillis(300), viewport, (int) Math.round(left + width * box[2]), (int) Math.round(top + height * box[3])));
        drag.addAction(pointer.createPointerUp(PointerInput.MouseButton.LEFT.asArg()));
        browser.perform(List.of(drag));
        visible(By.cssSelector("[role=img][aria-label='Выбранная область']"));
    }

    private void assertCaptionHighlight() {
        var highlight = visible(By.cssSelector(".react-pdf-highlight"));
        var canvas = visible(By.cssSelector(".react-pdf-frame canvas"));
        var normalizedTop = (Number) browser.executeScript("""
                const highlight = arguments[0].getBoundingClientRect();
                const canvas = arguments[1].getBoundingClientRect();
                return (highlight.top - canvas.top) / canvas.height;
                """, highlight, canvas);
        assertEquals(.220618, normalizedTop.doubleValue(), .005);
    }

    private void assertRegionPlan(String id, String parent, double[] expectedBox) throws Exception {
        var plan = regionPlan(id);
        var target = (Map<?, ?>) plan.get("target");
        assertEquals(Map.of("mode", "auto"), plan.get("selection"));
        assertEquals(parent, target.get("parent_run_id"));
        assertEquals(originalSha256("dev/D001.pdf"), target.get("pdf_sha256"));
        assertEquals(8, ((Number) target.get("page")).intValue());
        List<?> actualBox = (List<?>) target.get("box");
        for (int edge = 0; edge < expectedBox.length; edge++) {
            assertEquals(expectedBox[edge], ((Number) actualBox.get(edge)).doubleValue(), .003);
        }
    }

    private Map<?, ?> regionTarget(String id) throws Exception {
        return (Map<?, ?>) regionPlan(id).get("target");
    }

    private Map<?, ?> regionPlan(String id) throws Exception {
        var figure = (Map<?, ?>) ((List<?>) runCard(id).get("figures")).getFirst();
        return (Map<?, ?>) figure.get("plan");
    }

    private String currentRun() {
        var query = URI.create(browser.getCurrentUrl()).getQuery();
        var match = Pattern.compile("(?:^|&)run=([^&]+)").matcher(query);
        assertTrue(match.find(), query);
        return match.group(1);
    }

    private Map<?, ?> runCard(String id) throws Exception {
        byte[] bytes = download(origin.resolve("/api/article-runs/" + id));
        return json.toType(new String(bytes, StandardCharsets.UTF_8), Map.class);
    }

    private String runPath(String document) {
        return "/?lab=1&article=" + System.getenv("RAG_QA_" + document + "_ARTICLE_ID")
                + "&run=" + System.getenv("RAG_QA_" + document + "_RUN_ID");
    }

    private URI assertPinnedPublicationSource(String document, int page) throws Exception {
        visible(By.cssSelector("[role=img][aria-label='Страница " + page + " · подсвечен источник']"));
        URI pdf = URI.create(visible(By.linkText("Открыть PDF целиком")).getDomProperty("href"));
        assertEquals("page=" + page, pdf.getFragment());
        assertTrue(pdf.getPath().matches("/api/publications/[^/]+/versions/[^/]+/source\\.pdf"), pdf.toString());
        assertOriginalPdf(pdf, document);
        return pdf;
    }

    private URI assertPinnedRunSource(String run, String document, int page) throws Exception {
        visible(By.cssSelector("[role=img][aria-label='Страница " + page + " · подсвечен источник']"));
        URI pdf = URI.create(visible(By.linkText("Открыть PDF целиком")).getDomProperty("href"));
        assertEquals("/api/article-runs/" + run + "/source.pdf", pdf.getPath());
        assertEquals("page=" + page, pdf.getFragment());
        assertEquals("sha256=" + originalSha256(document), pdf.getQuery());
        assertOriginalPdf(pdf, document);
        return pdf;
    }

    private String originalSha256(String document) throws Exception {
        byte[] original = Files.readAllBytes(Path.of(System.getenv("RAG_QA_CORPUS"), document));
        return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(original));
    }

    private void assertOriginalPdf(URI pdf, String document) throws Exception {
        String actual = HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(download(pdf)));
        assertEquals(originalSha256(document), actual, "Исходный PDF и PDF из интерфейса");
    }

    private void assertPinnedZip(URI source, int expectedRows) throws Exception {
        URI export = URI.create(visible(By.linkText("Скачать ZIP · 7 CSV")).getDomProperty("href"));
        assertEquals(source.getPath().replace("source.pdf", "export.zip"), export.getPath());
        Map<String, String> entries = new HashMap<>();
        try (var zip = new ZipInputStream(new ByteArrayInputStream(download(export)))) {
            for (var entry = zip.getNextEntry(); entry != null; entry = zip.getNextEntry()) {
                entries.put(entry.getName(), new String(zip.readAllBytes(), StandardCharsets.UTF_8));
            }
        }
        Map<?, ?> manifest = json.toType(entries.get("manifest.json"), Map.class);
        assertEquals(export.getPath().split("/")[5], manifest.get("versionId"));
        assertTrue(visible(By.cssSelector("dialog")).getText().contains(manifest.get("versionId").toString()));
        assertEquals(7, entries.keySet().stream().filter(name -> name.endsWith(".csv")).count());
        assertTrue(entries.containsKey("PASSPORT.md"));
        Map<?, ?> report = json.toType(entries.get("report.json"), Map.class);
        var results = (Number) ((Map<?, ?>) report.get("rows")).get("results");
        assertEquals(expectedRows, results.intValue(), "Строки результатов по отчёту проверки");
    }

    private byte[] download(URI uri) throws Exception {
        var target = origin.resolve(uri.getRawPath() + (uri.getRawQuery() == null ? "" : "?" + uri.getRawQuery()));
        var request = HttpRequest.newBuilder(target).timeout(Duration.ofSeconds(10)).GET().build();
        var response = http.send(request, HttpResponse.BodyHandlers.ofByteArray());
        assertEquals(200, response.statusCode(), target.toString());
        return response.body();
    }

    private WebElement visible(By locator) {
        return waitFor().until(ExpectedConditions.visibilityOfElementLocated(locator));
    }

    private void click(By locator) {
        waitFor().until(ExpectedConditions.elementToBeClickable(locator)).click();
    }

    private WebDriverWait waitFor() {
        return new WebDriverWait(browser, Duration.ofSeconds(15));
    }

    private static By button(String label) {
        return By.xpath("//button[normalize-space(.)='" + label + "']");
    }

    private void screenshot(String title) {
        Allure.addAttachment(title, "image/png", new ByteArrayInputStream(browser.getScreenshotAs(OutputType.BYTES)), ".png");
    }
}
