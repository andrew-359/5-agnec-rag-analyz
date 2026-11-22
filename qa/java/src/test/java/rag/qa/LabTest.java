package rag.qa;

import io.qameta.allure.Allure;
import io.qameta.allure.Epic;
import io.qameta.allure.Feature;
import io.qameta.allure.Story;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.Tag;
import org.junit.jupiter.api.io.TempDir;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.CsvSource;
import org.openqa.selenium.By;
import org.openqa.selenium.Keys;
import org.openqa.selenium.OutputType;
import org.openqa.selenium.WebElement;
import org.openqa.selenium.chrome.ChromeDriver;
import org.openqa.selenium.chrome.ChromeDriverService;
import org.openqa.selenium.chrome.ChromeOptions;
import org.openqa.selenium.logging.LogType;
import org.openqa.selenium.logging.LoggingPreferences;
import org.openqa.selenium.support.ui.ExpectedConditions;
import org.openqa.selenium.support.ui.Select;
import org.openqa.selenium.support.ui.WebDriverWait;

import java.io.ByteArrayInputStream;
import java.io.IOException;
import java.net.URI;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.util.List;
import java.util.Map;
import java.util.zip.ZipFile;
import org.openqa.selenium.json.Json;
import java.util.function.Supplier;
import java.util.logging.Level;

import static org.junit.jupiter.api.Assertions.*;

@Epic("Браузерные проверки")
final class LabTest {
    private static final By SEARCH = By.cssSelector("input[type=search]");
    private static final By DATASET = By.xpath("//label[starts-with(normalize-space(.),'Датасет')]/select");
    private static final By PAGE_TWO = By.cssSelector("[role=img][aria-label='Страница 2 · подсвечен источник']");
    private static final By SOURCE_LINK = By.linkText("Открыть PDF целиком");
    private static final By ALERT = By.cssSelector("[role=alert]");

    private static final By PREVIOUS = By.cssSelector("button[aria-label='Предыдущая страница']");
    private static final By NEXT = By.cssSelector("button[aria-label='Следующая страница']");

    private static final By PROPOSED = By.xpath("//label[contains(.,'Предлагаемое значение')]/input");
    private static final By REASON = By.xpath("//label[contains(.,'Причина')]/textarea");

    @TempDir Path temporary;

    @Test
    @Feature("Источники PDF")
    @Story("Синтетические данные")
    @DisplayName("Результат открывает свой источник, фильтры и PDF восстанавливаются после обновления")
    void restoresExactSourceAfterReload() throws Throwable {
        scenario(Boolean.getBoolean("rag.qa.failSource"), (fixture, browser) -> {
            Allure.step("В библиотеке открываем результаты синтетической статьи", () -> {
                browser.get(fixture.origin().toString());
                click(browser, By.linkText("Результаты"));
                visible(browser, By.cssSelector("[data-result=R4]"));
            });
            Allure.step("Поиск по Alpha и фильтр HotpotQA оставляют две разные записи .422", () -> {
                visible(browser, SEARCH).sendKeys("Alpha .422");
                waitFor(browser).until(ExpectedConditions.invisibilityOfElementLocated(By.cssSelector("[data-result=R4]")));
                new Select(visible(browser, DATASET)).selectByVisibleText("HotpotQA");
                waitFor(browser).until(ExpectedConditions.invisibilityOfElementLocated(By.cssSelector("[data-result=R3]")));
                assertEquals(".422", visible(browser, By.cssSelector("[data-result=R1]")).getText());
                assertEquals(".422", visible(browser, By.cssSelector("[data-result=R2]")).getText());
            });
            Allure.step("Для записи R2 открываем подсвеченную страницу 2 закреплённого выпуска", () -> {
                click(browser, By.cssSelector("[data-result=R2]"));
                assertSource(browser);
                assertTrue(fixture.requests().contains("GET " + LabFixture.PDF_PATH),
                        "The browser should request the PDF of the selected publication version");
            });
            Allure.step("После обновления видны тот же фильтр, запрос и источник", () -> {
                browser.navigate().refresh();
                assertSource(browser);
                assertEquals("Alpha .422", visible(browser, SEARCH).getDomProperty("value"));
                assertEquals("HotpotQA", new Select(visible(browser, DATASET)).getFirstSelectedOption().getText());
                assertTrue(URI.create(browser.getCurrentUrl()).getQuery().contains("result=R2"));
            });
        });
    }

    @Test
    @Feature("Загрузка и прогон")
    @Story("Синтетические данные")
    @DisplayName("После отказа очереди повторно запускается разбор уже загруженного PDF")
    void retriesRunWithoutUploadingAgain() throws Throwable {
        scenario(false, (fixture, browser) -> {
            Path input = Files.write(temporary.resolve("upload.pdf"), fixture.inputPdf());
            Allure.step("Выбираем PDF и запускаем разбор в режиме «Авто»", () -> {
                browser.get(fixture.origin().toString());
                click(browser, By.linkText("Разобрать статью"));
                browser.findElement(By.cssSelector("input[type=file]")).sendKeys(input.toString());
                assertTrue(browser.findElement(By.id("parse-auto")).isSelected());
                click(browser, button("Запустить разбор"));
            });
            Allure.step("Очередь отвечает ошибкой, форма сохраняет загруженную статью", () -> {
                assertEquals(LabFixture.START_ERROR, visible(browser, ALERT).getText());
                visible(browser, By.xpath("//*[contains(text(),'Карта готова')]"));
            });
            Allure.step("Повторяем запуск и видим карточку прогона той же статьи", () -> {
                click(browser, button("Повторить запуск"));
                waitFor(browser).until(ExpectedConditions.textToBePresentInElementLocated(By.cssSelector("[role=status]"), "Идёт разбор"));
                String query = URI.create(browser.getCurrentUrl()).getQuery();
                assertTrue(query.contains("article=" + LabFixture.UPLOADED));
                assertTrue(query.contains("run=" + LabFixture.NEW_RUN));
                assertEquals(List.of("POST /api/articles", "POST /api/articles/" + LabFixture.UPLOADED + "/runs",
                        "POST /api/articles/" + LabFixture.UPLOADED + "/runs"),
                        fixture.requests().stream().filter(request -> request.startsWith("POST ")).toList());
            });
        });
    }

    @Test
    @Feature("Проверки и ручные правки")
    @Story("Синтетические данные")
    @DisplayName("Ручная правка сохраняется после перезагрузки и не меняет опубликованный результат")
    void restoresReviewDraft() throws Throwable {
        scenario(false, (fixture, browser) -> {
            Allure.step("Выбираем замечание к результату R2 и сохраняем предложение", () -> {
                openTab(browser, fixture, "Проверки");
                click(browser, button("Уточнить значение Alpha"));
                click(browser, button("Предложить исправление"));
                visible(browser, By.xpath("//label[contains(.,'Предлагаемое значение')]/input")).sendKeys("0.431");
                visible(browser, By.xpath("//label[contains(.,'Причина')]/textarea")).sendKeys("Сверено с таблицей.");
                click(browser, button("Сохранить черновик"));
                visible(browser, text("Сверено с таблицей."));
            });
            Allure.step("После обновления читаем сохранённый черновик и исходное значение", () -> {
                browser.navigate().refresh();
                visible(browser, text("Черновик · ожидает применения"));
                visible(browser, text("Сверено с таблицей."));
                visible(browser, By.xpath("//p[contains(.,'0.422 → 0.431')]"));
                click(browser, By.linkText("Результаты"));
                assertEquals(".422", visible(browser, By.cssSelector("[data-result=R2]")).getText());
            });
        });
    }

    @Test
    @Feature("Сравнение")
    @Story("Синтетические данные")
    @DisplayName("Сравнение открывает протокол и показывает разницу только для подтверждённой пары")
    void showsDeltaOnlyForComparablePair() throws Throwable {
        scenario(false, (fixture, browser) -> {
            Allure.step("Выбираем рекомендованную пару и открываем протокол на странице 2", () -> {
                openTab(browser, fixture, "Сравнения");
                click(browser, button("Сравнить"));
                visible(browser, text("Сопоставимость подтверждена"));
                visible(browser, text("0.078"));
                click(browser, button("Протокол в PDF"));
                assertSource(browser);
            });
            Allure.step("Подбираем другую запись и проверяем ответ без числовой разницы", () -> {
                click(browser, button("Заменить вторую запись"));
                click(browser, button("Проверить пару"));
                visible(browser, text("Недостаточно оснований"));
                visible(browser, text("Общая выборка не подтверждена."));
                assertTrue(browser.findElements(By.xpath("//*[contains(text(),'Абсолютная разница')]")).isEmpty());
            });
        });
    }

    @Test
    @Feature("Паспорт и выгрузка")
    @Story("Синтетические данные")
    @DisplayName("Паспорт скачивает ZIP закреплённой версии с семью CSV")
    void downloadsPinnedExport() throws Throwable {
        scenario(false, (fixture, browser) -> {
            Allure.step("Открываем паспорт и скачиваем архив указанного выпуска", () -> {
                browser.get(fixture.origin().toString());
                click(browser, By.linkText("Результаты"));
                click(browser, button("Паспорт и выгрузка"));
                assertPassportVersion(browser, LabFixture.VERSION);
                click(browser, By.partialLinkText("Скачать ZIP"));
            });
            Allure.step("Сверяем manifest.json и состав скачанного ZIP", () -> {
                Path archive = temporary.resolve("export.zip");
                waitFor(browser).until(driver -> Files.isRegularFile(archive));
                try (var zip = new ZipFile(archive.toFile())) {
                    assertEquals(7, zip.stream().filter(entry -> entry.getName().endsWith(".csv")).count());
                    assertNotNull(zip.getEntry("PASSPORT.md"));
                    assertNotNull(zip.getEntry("report.json"));
                    try (var manifest = zip.getInputStream(zip.getEntry("manifest.json"))) {
                        Map<?, ?> data = new Json().toType(new String(manifest.readAllBytes(),
                                java.nio.charset.StandardCharsets.UTF_8), Map.class);
                        assertEquals(LabFixture.VERSION, data.get("versionId"));
                        assertEquals("run-published", data.get("runId"));
                    }
                }
            });
        });
    }

    @Test
    @Feature("Библиотека и навигация")
    @Story("Синтетические данные")
    @DisplayName("NAV-01/02/04/05/09 · Библиотека, поиск и возврат из статьи")
    void navigatesLibrary() throws Throwable {
        scenario(false, (fixture, browser) -> {
            browser.get(fixture.origin().toString());
            visible(browser, By.xpath("//h1[.='Библиотека']"));
            visible(browser, By.linkText("Разобрать статью"));
            for (String query : List.of("  СИНТЕТИЧЕСКАЯ  ", "synthetic.pdf")) {
                replaceSearch(browser, query);
                visible(browser, text("Синтетическая статья"));
            }
            replaceSearch(browser, "Несуществующая статья");
            visible(browser, text("По запросу статей нет."));
            replaceSearch(browser, "");
            click(browser, By.xpath("//section[.//h2[.='Синтетическая статья']]//a[.='Открыть статью']"));
            visible(browser, text("О статье"));
            click(browser, By.cssSelector(".article-return a"));
            click(browser, By.xpath("//section[.//h2[.='Синтетическая статья']]//a[.='Открыть статью']"));
            click(browser, By.linkText("RAG"));
            visible(browser, text("Библиотека"));
            browser.get(fixture.origin() + "/?article=" + LabFixture.ARTICLE + "&tab=unknown&variant=unknown");
            visible(browser, By.cssSelector("a[aria-current=page][href*='tab=overview']"));
        });
    }

    @Test
    @Feature("Паспорт и выгрузка")
    @Story("Синтетические данные")
    @DisplayName("NAV-07 · Недоступный выпуск не подменяется актуальным")
    void refusesMissingVersion() throws Throwable {
        scenario(false, (fixture, browser) -> {
            browser.get(fixture.origin() + "/?article=" + LabFixture.ARTICLE + "&run=run-missing&tab=results&result=R2");
            visible(browser, ALERT);
            hidden(browser, result("R2"));
            hidden(browser, SOURCE_LINK);
        });
    }

    @ParameterizedTest(name = "{0} → {1}")
    @CsvSource({"ALPHA HotpotQA EM .422,R1 R2", "Beta,R4", "Synthetic model,R1 R2 R3 R4",
            "Other QA,R3", "F1,R5", ".500,R4"})
    @Feature("Поиск и фильтры")
    @Story("Синтетические данные")
    @DisplayName("RES-01/04 · Поиск по методу, генератору, датасету, метрике и числу")
    void searchesResultFields(String query, String expected) throws Throwable {
        scenario(false, (fixture, browser) -> {
            browser.get(resultsUrl(fixture));
            visible(browser, result("R5"));
            replaceSearch(browser, query);
            assertRows(browser, expected);
        });
    }

    @Test
    @Feature("Поиск и фильтры")
    @Story("Синтетические данные")
    @DisplayName("RES-02/05/11 · Комбинации фильтров и опубликованный ноль без генератора")
    void combinesFiltersAndPreservesZero() throws Throwable {
        scenario(false, (fixture, browser) -> {
            browser.get(resultsUrl(fixture));
            select(browser, DATASET, "HotpotQA");
            select(browser, filter("Метрика"), "F1");
            assertRows(browser, "R5");
            select(browser, DATASET, "Other QA");
            visible(browser, text("Нет результатов по этим фильтрам."));
            select(browser, DATASET, "Все");
            select(browser, filter("Генератор"), "Не установлен");
            assertRows(browser, "R5");
            assertEquals("0", visible(browser, result("R5")).getText());
            replaceSearch(browser, "Beta");
            visible(browser, text("Нет результатов по этим фильтрам."));
        });
    }

    @Test
    @Feature("Поиск и фильтры")
    @Story("Синтетические данные")
    @DisplayName("RES-06/07/08/09/12 · Поиск, история, прямой адрес и выбранный источник")
    void preservesOrClearsSourceWithFilters() throws Throwable {
        scenario(false, (fixture, browser) -> {
            browser.get(resultsUrl(fixture));
            click(browser, result("R2"));
            assertSource(browser);
            replaceSearch(browser, "Alpha");
            assertSource(browser);
            click(browser, By.cssSelector(".article-return a"));
            browser.navigate().back();
            assertSource(browser);
            assertEquals("Alpha", visible(browser, SEARCH).getDomProperty("value"));
            replaceSearch(browser, "Beta");
            hidden(browser, SOURCE_LINK);
            assertFalse(URI.create(browser.getCurrentUrl()).getQuery().contains("result=R2"));
            browser.get(resultsUrl(fixture) + "&result=R2&resultSearch=Beta");
            visible(browser, result("R4"));
            hidden(browser, SOURCE_LINK);
            browser.get(resultsUrl(fixture));
            select(browser, filter("Метрика"), "EM");
            click(browser, result("R2"));
            assertSource(browser);
            select(browser, DATASET, "Other QA");
            hidden(browser, SOURCE_LINK);
        });
    }

    @Test
    @Feature("Источники PDF")
    @Story("Синтетические данные")
    @DisplayName("SRC-01/02/03/04/05/06/11 · Одинаковые числа, страницы, фрагмент и фокус")
    void navigatesPdfAndRestoresFocus() throws Throwable {
        scenario(false, (fixture, browser) -> {
            browser.get(resultsUrl(fixture));
            click(browser, result("R1"));
            visible(browser, page(1, true));
            assertFalse(visible(browser, PREVIOUS).isEnabled());
            click(browser, NEXT);
            visible(browser, page(2, false));
            assertFalse(visible(browser, NEXT).isEnabled());
            visible(browser, PREVIOUS).sendKeys(Keys.ARROW_LEFT);
            visible(browser, page(1, true));
            visible(browser, NEXT).sendKeys(Keys.ARROW_RIGHT);
            visible(browser, page(2, false));
            click(browser, button("Фрагмент"));
            visible(browser, page(1, true));
            assertEquals("true", visible(browser, button("Фрагмент")).getDomAttribute("aria-pressed"));
            click(browser, button("Вся страница"));
            assertEquals("true", visible(browser, button("Вся страница")).getDomAttribute("aria-pressed"));
            click(browser, result("R2"));
            assertSource(browser);
            visible(browser, button("Закрыть PDF")).sendKeys(Keys.ENTER);
            hidden(browser, SOURCE_LINK);
            assertEquals("R2", browser.switchTo().activeElement().getDomAttribute("data-result"));
        });
    }

    @Test
    @Feature("Источники PDF")
    @Story("Синтетические данные")
    @DisplayName("SRC-07/08 · Источник без координат и повтор PDF после временного отказа")
    void recoversPdfAndSupportsPageOnlyEvidence() throws Throwable {
        scenario(false, (fixture, browser) -> {
            browser.get(resultsUrl(fixture));
            fixture.failNextPdf();
            click(browser, result("R2"));
            visible(browser, text("Не удалось загрузить страницу PDF."));
            click(browser, button("Повторить"));
            assertSource(browser);
            click(browser, result("R5"));
            visible(browser, page(1, false));
            visible(browser, text("Для источника доступна страница целиком."));
            assertFalse(visible(browser, button("Фрагмент")).isEnabled());
        });
    }

    @Test
    @Feature("Карта статьи")
    @Story("Синтетические данные")
    @DisplayName("MAP-01/02/03/05/07 · Карта открывает раздел, таблицу и рисунок в PDF")
    void opensMapAnchors() throws Throwable {
        scenario(false, (fixture, browser) -> {
            browser.get(fixture.origin() + "/?article=" + LabFixture.ARTICLE);
            click(browser, text("Карта статьи"));
            click(browser, button("Таблицы 1"));
            click(browser, text("Table 1"));
            visible(browser, page(1, true));
            assertEquals("/api/articles/" + LabFixture.ARTICLE + "/source.pdf",
                    URI.create(visible(browser, SOURCE_LINK).getDomProperty("href")).getPath());
            click(browser, NEXT);
            visible(browser, page(2, false));
            click(browser, button("Фрагмент"));
            visible(browser, page(1, true));
            click(browser, button("Разделы 1"));
            click(browser, text("Методика"));
            visible(browser, page(1, false));
            click(browser, button("Рисунки 1"));
            click(browser, text("Figure 3"));
            assertEquals("true", visible(browser, text("Figure 3")).findElement(By.xpath("..")).getDomAttribute("aria-pressed"));
            visible(browser, page(2, true));
            click(browser, text("Карта статьи"));
            hidden(browser, text("Figure 3"));
            visible(browser, page(2, true));
            click(browser, text("Карта статьи"));
            visible(browser, text("Figure 3"));
        });
    }

    @Test
    @Feature("Карта статьи")
    @Story("Синтетические данные")
    @DisplayName("MAP-06 · Пустая карта даёт понятное сообщение")
    void showsEmptyMap() throws Throwable {
        scenario(false, (fixture, browser) -> {
            browser.get(fixture.origin() + "/?article=" + LabFixture.UPLOADED);
            click(browser, text("Карта статьи"));
            visible(browser, text("На карте пока нет разделов, таблиц или рисунков."));
        });
    }

    @Test
    @Feature("Проверки и ручные правки")
    @Story("Синтетические данные")
    @DisplayName("REV-01/02/04/11 · Связанная запись, несколько ячеек и замечание без источника")
    void opensLinkedAndUnlinkedRemarks() throws Throwable {
        scenario(false, (fixture, browser) -> {
            openTab(browser, fixture, "Проверки");
            click(browser, button("Уточнить значение Alpha"));
            assertSource(browser);
            click(browser, button("← К замечаниям"));
            assertEquals("Уточнить значение Alpha", browser.switchTo().activeElement().getText());
            click(browser, button("Две связанные ячейки"));
            visible(browser, page(1, true));
            new Select(visible(browser, filter("Связанный результат"))).selectByValue("R2");
            assertSource(browser);
            click(browser, By.linkText("Alpha · .422"));
            assertSource(browser);
            click(browser, By.linkText("Проверки"));
            click(browser, button("Условие не установлено"));
            visible(browser, text("У замечания нет доступного фрагмента."));
            visible(browser, text("Замечание пока не связано с опубликованной записью."));
            hidden(browser, SOURCE_LINK);
        });
    }

    @Test
    @Feature("Сравнение")
    @Story("Синтетические данные")
    @DisplayName("CMP-03/07/08/13 · Обе ячейки, условия, изменение пары и закреплённый адрес")
    void opensComparisonSourcesAndRestoresPair() throws Throwable {
        scenario(false, (fixture, browser) -> {
            openTab(browser, fixture, "Сравнения");
            click(browser, button("Сравнить"));
            click(browser, text("Условия сравнения"));
            visible(browser, text("Проверка"));
            click(browser, button("Источник Beta · стр. 1"));
            visible(browser, page(1, true));
            click(browser, button("Источник Alpha · стр. 2"));
            assertSource(browser);
            click(browser, PREVIOUS);
            visible(browser, page(1, false));
            browser.navigate().refresh();
            visible(browser, text("Сопоставимость подтверждена"));
            assertTrue(URI.create(browser.getCurrentUrl()).getQuery().contains("compareB=R4"));
            click(browser, button("Заменить вторую запись"));
            click(browser, button("В других статьях"));
            visible(browser, text("Подходящих кандидатов с тем же датасетом и метрикой нет."));
            click(browser, By.linkText("Результаты"));
            visible(browser, result("R2"));
            hidden(browser, text("Сопоставимость подтверждена"));
        });
    }

    @Test
    @Feature("Сравнение")
    @Story("Синтетические данные")
    @DisplayName("CMP-10 · Предложения можно загрузить повторно после 503")
    void retriesRecommendations() throws Throwable {
        scenario(false, (fixture, browser) -> {
            fixture.failNextRecommendation();
            openTab(browser, fixture, "Сравнения");
            visible(browser, text("Подбор временно недоступен."));
            click(browser, button("Повторить"));
            click(browser, button("Сравнить"));
            visible(browser, text("Сопоставимость подтверждена"));
        });
    }

    @Test
    @Feature("Паспорт и выгрузка")
    @Story("Синтетические данные")
    @DisplayName("EXP-01/02/03/07/08/09 · Паспорт, повтор загрузки, источник и клавиатура")
    void previewsPassportAndOpensControlCell() throws Throwable {
        scenario(false, (fixture, browser) -> {
            browser.get(resultsUrl(fixture));
            visible(browser, result("R2"));
            fixture.failNextPassport();
            click(browser, button("Паспорт и выгрузка"));
            visible(browser, text("Не удалось загрузить паспорт."));
            hidden(browser, By.partialLinkText("Скачать ZIP"));
            click(browser, button("Повторить"));
            visible(browser, text("Частичный охват"));
            for (String title : List.of("Связи и типы", "Поля в CSV", "Охват и сведения о версии")) {
                click(browser, text(title));
                assertNotNull(visible(browser, text(title)).findElement(By.xpath("..")).getDomAttribute("open"));
            }
            browser.switchTo().activeElement().sendKeys(Keys.ESCAPE);
            hidden(browser, By.cssSelector("dialog[open]"));
            click(browser, button("Паспорт и выгрузка"));
            click(browser, By.cssSelector("button[aria-label='Закрыть паспорт']"));
            hidden(browser, By.cssSelector("dialog[open]"));
            replaceSearch(browser, "Beta");
            click(browser, button("Паспорт и выгрузка"));
            click(browser, By.linkText("Открыть контрольную ячейку в PDF"));
            assertSource(browser);
            assertEquals("", visible(browser, SEARCH).getDomProperty("value"));
        });
    }

    @Test
    @Feature("Проверки и ручные правки")
    @Story("Синтетические данные")
    @DisplayName("REV-05/06 · Исправление единицы и восстановление черновика после переходов")
    void savesUnitDraftAcrossHistory() throws Throwable {
        scenario(false, (fixture, browser) -> {
            openTab(browser, fixture, "Проверки");
            click(browser, button("Уточнить значение Alpha"));
            click(browser, button("Предложить исправление"));
            new Select(visible(browser, filter("Поле"))).selectByValue("unit");
            visible(browser, PROPOSED).sendKeys("percent");
            visible(browser, REASON).sendKeys("Единица напечатана в подписи.");
            click(browser, button("Сохранить черновик"));
            visible(browser, text("Единица напечатана в подписи."));
            click(browser, By.linkText("Результаты"));
            browser.navigate().back();
            visible(browser, text("Единица напечатана в подписи."));
            visible(browser, By.xpath("//p[contains(.,'fraction → percent')]"));
            browser.navigate().forward();
            visible(browser, result("R2"));
        });
    }

    @Test
    @Feature("Проверки и ручные правки")
    @Story("Синтетические данные")
    @DisplayName("REV-07/08 · Требуется значение и причина, переход просит подтвердить потерю правки")
    void validatesAndProtectsUnsavedDraft() throws Throwable {
        scenario(false, (fixture, browser) -> {
            openTab(browser, fixture, "Проверки");
            click(browser, button("Уточнить значение Alpha"));
            click(browser, button("Предложить исправление"));
            click(browser, button("Сохранить черновик"));
            assertEquals("true", visible(browser, PROPOSED).getDomProperty("required"));
            visible(browser, PROPOSED).sendKeys("0.5");
            click(browser, button("Сохранить черновик"));
            assertEquals("true", visible(browser, REASON).getDomProperty("required"));
            assertFalse(fixture.requests().contains("POST /api/review/drafts"));
            click(browser, button("← К замечаниям"));
            waitFor(browser).until(ExpectedConditions.alertIsPresent()).dismiss();
            assertEquals("0.5", visible(browser, PROPOSED).getDomProperty("value"));
            click(browser, button("← К замечаниям"));
            waitFor(browser).until(ExpectedConditions.alertIsPresent()).accept();
            hidden(browser, PROPOSED);
            click(browser, button("Условие не установлено"));
            visible(browser, text("У замечания нет доступного фрагмента."));
        });
    }

    @Test
    @Feature("Проверки и ручные правки")
    @Story("Синтетические данные")
    @DisplayName("REV-09 · Оставить условие открытым, восстановить и удалить черновик")
    void leavesUnlinkedRemarkOpenAndDeletesDraft() throws Throwable {
        scenario(false, (fixture, browser) -> {
            openTab(browser, fixture, "Проверки");
            click(browser, button("Условие не установлено"));
            click(browser, button("Оставить открытым"));
            visible(browser, REASON).sendKeys("Единица в статье пропущена.");
            click(browser, button("Сохранить причину"));
            visible(browser, text("Оставлено открытым"));
            browser.navigate().refresh();
            visible(browser, text("Единица в статье пропущена."));
            click(browser, button("Удалить черновик"));
            waitFor(browser).until(ExpectedConditions.alertIsPresent()).accept();
            hidden(browser, text("Оставлено открытым"));
            browser.navigate().refresh();
            visible(browser, button("Оставить открытым"));
            assertTrue(fixture.requestLog().contains("DELETE /api/review/drafts/draft-1 → 204"));
        });
    }

    @Test
    @Feature("Сравнение")
    @Story("Синтетические данные")
    @DisplayName("CMP-01/05/11 · Рекомендации, несовместимая метрика и неизвестный адрес")
    void checksRecommendationsAndInvalidPairs() throws Throwable {
        scenario(false, (fixture, browser) -> {
            openTab(browser, fixture, "Сравнения");
            click(browser, By.xpath("//summary[normalize-space(.)='Другие пары · 1']"));
            click(browser, By.cssSelector(".comparison-recommendations details button"));
            visible(browser, text("Сопоставимость подтверждена"));
            assertTrue(URI.create(browser.getCurrentUrl()).getQuery().contains("compareA=R1"));
            String comparison = resultsUrl(fixture).replace("tab=results", "tab=compare");
            browser.get(comparison + "&compareA=R2&compareB=R5");
            visible(browser, text("Условия различаются"));
            visible(browser, text("Метрики различаются."));
            hidden(browser, By.cssSelector(".comparison-delta"));
            browser.get(comparison + "&compareA=R2&compareB=R999");
            visible(browser, text("Запись не найдена в закреплённой версии."));
            browser.get(comparison + "&compareA=R2");
            visible(browser, text("Подбор второй записи"));
            hidden(browser, By.cssSelector(".comparison-verdict"));
        });
    }

    @Test
    @Feature("Библиотека и навигация")
    @Story("Синтетические данные")
    @DisplayName("CMP-09/NAV-06 · Пустые рекомендации и открытие другой статьи без старого состояния")
    void keepsArticleStateIndependent() throws Throwable {
        scenario(false, (fixture, browser) -> {
            fixture.hideRecommendations();
            browser.get(resultsUrl(fixture));
            visible(browser, result("R2"));
            replaceSearch(browser, "Alpha");
            click(browser, result("R2"));
            click(browser, By.linkText("Подобрать сравнение для .422"));
            visible(browser, text("Для этого выпуска нет подтверждённых пар."));
            visible(browser, text("Подбор второй записи"));
            click(browser, By.cssSelector(".article-return a"));
            click(browser, By.xpath("//section[.//h2[.='Другая статья']]//a[.='Открыть статью']"));
            visible(browser, text("О статье"));
            String query = URI.create(browser.getCurrentUrl()).getQuery();
            assertTrue(query.contains("article=" + LabFixture.UPLOADED));
            assertFalse(query.contains("resultSearch"));
            assertFalse(query.contains("compareA"));
            hidden(browser, SOURCE_LINK);
        });
    }

    @ParameterizedTest(name = "{0}×{1}")
    @CsvSource({"1440,900", "1280,800"})
    @Feature("Интерфейс и доступность")
    @Story("Синтетические данные")
    @DisplayName("UX-03/07 · Поиск, PDF и доступный паспорт на двух размерах экрана")
    void fitsDesktopViewport(int width, int height) throws Throwable {
        scenario(false, (fixture, browser) -> {
            browser.manage().window().setSize(new org.openqa.selenium.Dimension(width, height));
            browser.get(resultsUrl(fixture));
            visible(browser, SEARCH);
            visible(browser, result("R2")).sendKeys(Keys.ENTER);
            assertSource(browser);
            click(browser, button("Закрыть PDF"));
            visible(browser, button("Паспорт и выгрузка")).sendKeys(Keys.ENTER);
            var dialog = visible(browser, By.cssSelector("dialog[open]"));
            assertEquals("Паспорт данных", browser.findElement(By.id(dialog.getDomAttribute("aria-labelledby"))).getText());
            assertEquals("Закрыть паспорт", browser.switchTo().activeElement().getAccessibleName());
            browser.switchTo().activeElement().sendKeys(Keys.ESCAPE);
            hidden(browser, By.cssSelector("dialog[open]"));
        });
    }

    @Test
    @Feature("Источники PDF")
    @Story("Синтетические данные")
    @DisplayName("RES-10/SRC-12/REV-10 · Две записи из одного места и два замечания к одной ячейке")
    void distinguishesRecordsWithSharedEvidence() throws Throwable {
        scenario(false, (fixture, browser) -> {
            browser.get(resultsUrl(fixture));
            assertEquals(".238", visible(browser, result("R6")).getText());
            assertEquals("↓ 18.4%", visible(browser, result("R7")).getText());
            click(browser, result("R6"));
            assertSource(browser);
            click(browser, result("R7"));
            assertSource(browser);
            assertTrue(URI.create(browser.getCurrentUrl()).getQuery().contains("result=R7"));
            click(browser, By.linkText("Проверки"));
            click(browser, button("Уточнить значение Alpha"));
            assertSource(browser);
            assertTrue(URI.create(browser.getCurrentUrl()).getQuery().contains("remark=I1"));
            click(browser, button("← К замечаниям"));
            click(browser, button("Проверить шкалу Alpha"));
            assertSource(browser);
            assertTrue(URI.create(browser.getCurrentUrl()).getQuery().contains("remark=I4"));
            visible(browser, By.xpath("//article//h3[.='Проверить шкалу Alpha']"));
        });
    }

    @Test
    @Feature("Сравнение")
    @Story("Синтетические данные")
    @DisplayName("CMP-12 · Запоздавший вердикт не подменяет выбранную пару")
    void ignoresLateComparison() throws Throwable {
        scenario(false, (fixture, browser) -> {
            var delayed = fixture.delayNextComparison();
            openTab(browser, fixture, "Сравнения");
            click(browser, button("Сравнить"));
            visible(browser, text("Проверяем условия…"));
            assertTrue(delayed.awaitRequest());
            click(browser, By.xpath("//summary[normalize-space(.)='Другие пары · 1']"));
            click(browser, By.cssSelector(".comparison-recommendations details button"));
            visible(browser, text("Сопоставимость подтверждена"));
            delayed.release();
            waitFor(browser).until(driver -> fixture.requests().stream().filter("POST /api/comparisons"::equals).count() == 2);
            click(browser, button("Протокол в PDF"));
            visible(browser, page(1, true));
            assertTrue(URI.create(browser.getCurrentUrl()).getQuery().contains("compareA=R1"));
        });
    }

    @Test
    @Feature("Паспорт и выгрузка")
    @Story("Синтетические данные")
    @DisplayName("EXP-10 · Повторное открытие паспорта во время задержанного запроса")
    void reopensPassportWhileLoading() throws Throwable {
        scenario(false, (fixture, browser) -> {
            var delayed = fixture.delayNextPassport();
            browser.get(resultsUrl(fixture));
            click(browser, button("Паспорт и выгрузка"));
            visible(browser, text("Загружаем паспорт…"));
            assertTrue(delayed.awaitRequest());
            click(browser, By.cssSelector("button[aria-label='Закрыть паспорт']"));
            hidden(browser, By.cssSelector("dialog[open]"));
            click(browser, button("Паспорт и выгрузка"));
            delayed.release();
            assertPassportVersion(browser, LabFixture.VERSION);
            assertEquals(1, browser.findElements(By.cssSelector("dialog[open]")).size());
            assertEquals(LabFixture.RELEASE + "/export.zip", URI.create(
                    visible(browser, By.partialLinkText("Скачать ZIP")).getDomProperty("href")).getPath());
        });
    }

    @Test
    @Tag("live")
    @Tag("d001")
    @Feature("Паспорт и выгрузка")
    @Story("Живой стенд")
    @DisplayName("E2E-01 · D001 — найти R235, открыть PDF, сравнить с RA-CoT и скачать тот же выпуск")
    void checksRealArticleResultAndExport() throws Throwable {
        withBrowser(null, browser -> {
            Allure.step("Открываем R235 из поиска и сверяем оригинальную страницу 6", () -> {
                browser.get(liveArticle("results"));
                replaceSearch(browser, "ThreadRAG HotpotQA EM .422");
                assertEquals(".422", visible(browser, result("R235")).getText());
                click(browser, result("R235"));
                assertLiveSource(browser, 6);
                assertTrue(URI.create(browser.getCurrentUrl()).getQuery().contains("result=R235"));
            });
            Allure.step("Открываем подтверждённую пару RA-CoT и ThreadRAG и её источник", () -> {
                click(browser, By.linkText("Сравнения"));
                browser.get(liveArticle("compare") + "&compareA=R1&compareB=R235");
                visible(browser, text("Сопоставимость подтверждена"));
                visible(browser, text("0.218"));
                visible(browser, text(".204"));
                visible(browser, text(".422"));
                click(browser, button("Протокол в PDF"));
                assertLiveSource(browser, 6);
            });
            Allure.step("Скачиваем паспортный ZIP и сверяем байты закреплённого выпуска", () -> {
                try (var zip = downloadLiveArchive(browser)) {
                    assertNotNull(zip.getEntry("results.csv"));
                    assertEquals(7, zip.stream().filter(entry -> entry.getName().endsWith(".csv")).count());
                }
            });
        });
    }

    @Test
    @Tag("live")
    @Tag("d001")
    @Feature("Проверки и ручные правки")
    @Story("Живой стенд")
    @DisplayName("E2E-02 · D001 — замечание о снижении ведёт к R257, странице 7 и строке I4 в ZIP")
    void checksRealRemarkAndItsCsv() throws Throwable {
        withBrowser(null, browser -> {
            Allure.step("Открываем замечание о снижении EM и связанную запись", () -> {
                browser.get(liveArticle("checks"));
                click(browser, button("Снижение EM составляет 18.4 п.п. по разности 0.422 и 0.238."));
                visible(browser, text("↓ 18.4%"));
                assertLiveSource(browser, 7);
                click(browser, By.linkText("ThreadRAG · ↓ 18.4%"));
                assertLiveSource(browser, 7);
                assertTrue(URI.create(browser.getCurrentUrl()).getQuery().contains("result=R257"));
            });
            Allure.step("Выгруженный issues.csv содержит связь I4 с R257 и страницу 7", () -> {
                try (var zip = downloadLiveArchive(browser);
                     var issues = zip.getInputStream(zip.getEntry("issues.csv"))) {
                    String csv = new String(issues.readAllBytes(), java.nio.charset.StandardCharsets.UTF_8);
                    assertTrue(csv.lines().anyMatch(line -> line.startsWith("D001,I4,R257,scale-interpretation,")
                            && line.endsWith(",7,.238 (↓ 18.4%)")));
                }
            });
        });
    }

    @Test
    @Tag("live")
    @Tag("d001")
    @Feature("Сравнение")
    @Story("Живой стенд")
    @DisplayName("E2E-05 · D001 — два одинаковых числа из разных экспериментов без разницы")
    void checksRealIncomparablePair() throws Throwable {
        withBrowser(null, browser -> {
            browser.get(liveArticle("compare") + "&compareA=R235&compareB=R253");
            visible(browser, text("Недостаточно оснований"));
            assertEquals(2, browser.findElements(By.cssSelector(".comparison-item .big-value")).stream()
                    .filter(value -> ".422".equals(value.getText())).count());
            hidden(browser, By.cssSelector(".comparison-delta"));
            click(browser, button("Источник ThreadRAG · стр. 6"));
            assertLiveSource(browser, 6);
            click(browser, button("Источник ThreadRAG · стр. 7"));
            assertLiveSource(browser, 7);
        });
    }

    @Test
    @Tag("live")
    @Tag("d013")
    @Feature("Источники PDF")
    @Story("Живой стенд")
    @DisplayName("E2E-03 · D013 — среднее и SD выбираются отдельно и подсвечивают одну ячейку")
    void distinguishesRealMeanAndStandardDeviation() throws Throwable {
        withBrowser(null, browser -> {
            browser.get(liveArticle("results"));
            assertEquals("30.6", visible(browser, result("R1")).getText());
            assertEquals(".86", visible(browser, result("R2")).getText());
            click(browser, result("R1"));
            assertLiveSource(browser, 6);
            var meanBox = visible(browser, By.cssSelector(".react-pdf-highlight")).getDomAttribute("style");
            click(browser, text("Значение и условия"));
            visible(browser, By.xpath("//dt[.='Статистика']/following-sibling::dd[.='mean']"));
            assertTrue(URI.create(browser.getCurrentUrl()).getQuery().contains("result=R1"));
            click(browser, result("R2"));
            assertLiveSource(browser, 6);
            assertEquals(meanBox, visible(browser, By.cssSelector(".react-pdf-highlight")).getDomAttribute("style"));
            click(browser, text("Значение и условия"));
            visible(browser, By.xpath("//dt[.='Статистика']/following-sibling::dd[.='sample_sd']"));
            assertTrue(URI.create(browser.getCurrentUrl()).getQuery().contains("result=R2"));
        });
    }

    @Test
    @Tag("live")
    @Tag("p056")
    @Feature("Паспорт и выгрузка")
    @Story("Живой стенд")
    @DisplayName("P056 · Найти F1 PolyRAG, открыть ячейку скана и скачать ZIP выпуска")
    void opensRealScanEvidenceAndExport() throws Throwable {
        withBrowser(null, browser -> {
            browser.get(liveArticle("results"));
            replaceSearch(browser, "PolyRAG GPT-4 AcadChall F1 0.8109");
            assertEquals("0.8109", visible(browser, result("R26")).getText());
            click(browser, result("R26"));
            assertLiveSource(browser, 8);
            try (var zip = downloadLiveArchive(browser);
                 var results = zip.getInputStream(zip.getEntry("results.csv"))) {
                String csv = new String(results.readAllBytes(), java.nio.charset.StandardCharsets.UTF_8);
                assertTrue(csv.lines().anyMatch(line -> line.startsWith("P056,R26,") && line.contains("0.8109")));
            }
        });
    }

    @Test
    @Tag("live")
    @Tag("d001-archive")
    @Feature("Паспорт и выгрузка")
    @Story("Живой стенд")
    @DisplayName("E2E-04 · Архивный D001 сохраняет свою ячейку и ZIP после появления нового выпуска")
    void keepsRealArchivePinnedWhenLatestChanges() throws Throwable {
        withBrowser(null, browser -> {
            Allure.step("API уже показывает более свежую версию по адресу без закрепления", () -> {
                var input = new Json().toJson(Map.of("publicationId", liveProperty("publication")));
                var endpoint = URI.create(liveProperty("origin") + "/trpc/publications.review?input="
                        + java.net.URLEncoder.encode(input, java.nio.charset.StandardCharsets.UTF_8));
                try (var client = java.net.http.HttpClient.newHttpClient()) {
                    var response = client.send(java.net.http.HttpRequest.newBuilder(endpoint).timeout(Duration.ofSeconds(10)).build(),
                            java.net.http.HttpResponse.BodyHandlers.ofString());
                    assertEquals(200, response.statusCode());
                    Map<?, ?> responseData = new Json().toType(response.body(), Map.class);
                    var published = (Map<?, ?>) ((Map<?, ?>) responseData.get("result")).get("data");
                    assertEquals(liveProperty("newer"), ((Map<?, ?>) published.get("version")).get("versionId"));
                }
            });
            Allure.step("Старая ссылка остаётся на своём результате и скачивает исходные байты", () -> {
                browser.get(liveArticle("results") + "&result=R235");
                assertLiveSource(browser, 6);
                try (var zip = downloadLiveArchive(browser)) {
                    assertNotNull(zip.getEntry("manifest.json"));
                }
            });
        });
    }

    private static String liveArticle(String tab) {
        return liveProperty("origin") + "/?lab=1&article=" + liveProperty("article") + "&run="
                + liveProperty("run") + "&tab=" + tab;
    }

    private static String liveProperty(String key) {
        return System.getProperty("rag.qa.live." + key);
    }

    private static String liveRelease() {
        return "/api/publications/" + liveProperty("publication") + "/versions/" + liveProperty("version");
    }

    private static void assertLiveSource(ChromeDriver browser, int number) {
        visible(browser, page(number, true));
        URI pdf = URI.create(visible(browser, SOURCE_LINK).getDomProperty("href"));
        assertEquals(liveRelease() + "/source.pdf", pdf.getPath());
        assertEquals("page=" + number, pdf.getFragment());
    }

    private ZipFile downloadLiveArchive(ChromeDriver browser) throws IOException {
        click(browser, button("Паспорт и выгрузка"));
        assertPassportVersion(browser, liveProperty("version"));
        var download = By.partialLinkText("Скачать ZIP");
        assertEquals(liveRelease() + "/export.zip", URI.create(visible(browser, download).getDomProperty("href")).getPath());
        click(browser, download);
        Path archive = temporary.resolve("export.zip");
        waitFor(browser).until(driver -> Files.isRegularFile(archive));
        assertEquals(-1L, Files.mismatch(Path.of(liveProperty("zip")), archive));
        return new ZipFile(archive.toFile());
    }

    private static void assertPassportVersion(ChromeDriver browser, String version) {
        click(browser, By.cssSelector(".passport-version-id summary"));
        assertEquals(version, visible(browser, By.cssSelector(".passport-version-id code")).getText());
    }

    private static By result(String id) {
        return By.cssSelector("[data-result=" + id + "]");
    }

    private static By filter(String label) {
        return By.xpath("//label[starts-with(normalize-space(.),'" + label + "')]/select");
    }

    private static By page(int index, boolean highlighted) {
        return By.cssSelector("[role=img][aria-label='Страница " + index + (highlighted ? " · подсвечен источник" : "") + "']");
    }

    private static String resultsUrl(LabFixture fixture) {
        return fixture.origin() + "/?article=" + LabFixture.ARTICLE + "&run=run-published&tab=results";
    }

    private static void hidden(ChromeDriver browser, By locator) {
        waitFor(browser).until(ExpectedConditions.invisibilityOfElementLocated(locator));
    }

    private static void select(ChromeDriver browser, By locator, String value) {
        new Select(visible(browser, locator)).selectByVisibleText(value);
    }

    private static void replaceSearch(ChromeDriver browser, String query) {
        visible(browser, SEARCH).sendKeys(Keys.chord(Keys.CONTROL, "a"), Keys.BACK_SPACE, query);
    }

    private static void assertRows(ChromeDriver browser, String expected) {
        var ids = List.of(expected.split(" "));
        waitFor(browser).until(driver -> driver.findElements(By.cssSelector("[data-result]")).stream()
                .map(row -> row.getDomAttribute("data-result")).toList().equals(ids));
    }

    private void openTab(ChromeDriver browser, LabFixture fixture, String tab) {
        browser.get(fixture.origin().toString());
        click(browser, By.linkText("Результаты"));
        click(browser, By.linkText(tab));
    }

    private static By text(String value) {
        return By.xpath("//*[text()='" + value + "']");
    }

    private void assertSource(ChromeDriver browser) {
        waitFor(browser).withMessage("Ожидалась видимая подсветка ячейки R2 на странице 2.")
                .until(ExpectedConditions.or(ExpectedConditions.visibilityOfElementLocated(PAGE_TWO),
                        ExpectedConditions.visibilityOfElementLocated(ALERT)));
        browser.findElements(ALERT).stream().filter(WebElement::isDisplayed).findFirst().ifPresent(alert ->
                fail("Вместо подсвеченной страницы 2 интерфейс показал ошибку: " + alert.getText()));
        URI pdf = URI.create(visible(browser, SOURCE_LINK).getDomProperty("href"));
        assertEquals(LabFixture.PDF_PATH, pdf.getPath());
        assertEquals("page=2", pdf.getFragment());
    }

    private void scenario(boolean rejectSource, BrowserScenario scenario) throws Throwable {
        try (var fixture = new LabFixture(rejectSource)) {
            withBrowser(fixture, browser -> scenario.run(fixture, browser));
        }
    }

    private void withBrowser(LabFixture fixture, LiveBrowserScenario scenario) throws Throwable {
        Path driverLog = temporary.resolve("chromedriver.log");
        ChromeDriver browser = null;
        Throwable original = null;
        try {
            var service = new ChromeDriverService.Builder().withLogFile(driverLog.toFile());
            String driverPath = System.getenv("CHROMEDRIVER");
            if (driverPath != null && !driverPath.isBlank()) service.usingDriverExecutable(Path.of(driverPath).toFile());
            var logs = new LoggingPreferences();
            logs.enable(LogType.BROWSER, Level.ALL);
            var options = new ChromeOptions().addArguments("--headless=new", "--window-size=1440,1000", "--disable-dev-shm-usage");
            options.setCapability("goog:loggingPrefs", logs);
            options.setExperimentalOption("prefs", Map.of("download.default_directory", temporary.toString()));
            String chromePath = System.getenv("CHROME_BIN");
            if (chromePath != null && !chromePath.isBlank()) options.setBinary(chromePath);
            browser = new ChromeDriver(service.build(), options);
            attachText("Версии Chrome и ChromeDriver", browser.getCapabilities()::toString);
            scenario.run(browser);
        } catch (Throwable failure) {
            original = failure;
            capture(fixture, browser, driverLog);
            throw failure;
        } finally {
            try {
                if (browser != null) browser.quit();
            } catch (RuntimeException cleanupError) {
                if (original == null) throw cleanupError;
                original.addSuppressed(cleanupError);
            }
        }
    }

    @FunctionalInterface
    private interface LiveBrowserScenario {
        void run(ChromeDriver browser) throws Exception;
    }

    private void capture(LabFixture fixture, ChromeDriver browser, Path driverLog) {
        if (browser != null) {
            try {
                byte[] png = browser.getScreenshotAs(OutputType.BYTES);
                Allure.addAttachment("Скриншот Chrome", "image/png", new ByteArrayInputStream(png), ".png");
            } catch (RuntimeException ignored) {
                // A stopped browser may be unable to capture a screenshot.
            }
            attachText("Текущий URL", browser::getCurrentUrl);
            attachText("Фрагмент DOM", () -> {
                String dom = browser.findElement(By.tagName("main")).getDomProperty("outerHTML");
                return dom.substring(0, Math.min(dom.length(), 12000));
            });
            attachText("Консоль Chrome", () -> browser.manage().logs().get(LogType.BROWSER).getAll().toString());
        }
        if (fixture != null) attachText("Запросы стенда", fixture::requestLog);
        if (Files.isRegularFile(driverLog)) {
            try {
                String log = Files.readString(driverLog);
                attachText("Журнал ChromeDriver", () -> log.substring(Math.max(0, log.length() - 12000)));
            } catch (IOException ignored) {
                // The original test failure remains the cause.
            }
        }
    }

    private void attachText(String title, Supplier<String> value) {
        try {
            Allure.addAttachment(title, "text/plain", value.get(), ".txt");
        } catch (RuntimeException ignored) {
            // Diagnostics are collected independently.
        }
    }

    private static WebDriverWait waitFor(ChromeDriver browser) {
        return new WebDriverWait(browser, Duration.ofSeconds(12));
    }

    private static WebElement visible(ChromeDriver browser, By locator) {
        return waitFor(browser).until(ExpectedConditions.visibilityOfElementLocated(locator));
    }

    private static void click(ChromeDriver browser, By locator) {
        waitFor(browser).until(ExpectedConditions.elementToBeClickable(locator)).click();
    }

    private static By button(String label) {
        return By.xpath("//button[normalize-space(.)='" + label + "']");
    }

    @FunctionalInterface
    private interface BrowserScenario {
        void run(LabFixture fixture, ChromeDriver browser) throws Exception;
    }
}
