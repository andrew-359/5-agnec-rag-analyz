package rag.qa;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import org.openqa.selenium.json.Json;

import java.io.IOException;
import java.io.ByteArrayOutputStream;
import java.net.InetSocketAddress;
import java.net.URI;
import java.net.URLDecoder;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.HashMap;
import java.util.zip.ZipEntry;
import java.util.zip.ZipOutputStream;
import java.util.Map;
import java.util.Queue;
import java.util.concurrent.ConcurrentLinkedQueue;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.Executors;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicBoolean;

final class LabFixture implements AutoCloseable {
    static final String START_ERROR = "Тестовая очередь временно недоступна.";
    static final String ARTICLE = "article-synthetic";
    static final String UPLOADED = "article-uploaded";
    static final String NEW_RUN = "run-uploaded";
    static final String VERSION = "version-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
    static final String RELEASE = "/api/publications/publication-synthetic/versions/" + VERSION;
    static final String PDF_PATH = RELEASE + "/source.pdf";

    private final Json json = new Json();
    private final Map<?, ?> publication = json.toType(new String(resource("/publication.json"), StandardCharsets.UTF_8), Map.class);
    private final Map<?, ?> version = (Map<?, ?>) publication.get("version");
    private final String sha = (String) version.get("pdfSha256");
    private final byte[] pdf = resource("/synthetic.pdf");
    private final Path assets = Path.of(System.getProperty("rag.lab.assets"));
    private record Request(String route, int status) {}
    private final Queue<Request> requests = new ConcurrentLinkedQueue<>();
    private final AtomicInteger starts = new AtomicInteger();
    private final boolean rejectSource;
    private final AtomicBoolean nextPdfFails = new AtomicBoolean();
    private final AtomicBoolean nextPassportFails = new AtomicBoolean();
    private final AtomicBoolean nextRecommendationFails = new AtomicBoolean();
    private volatile boolean emptyRecommendations;
    private final HttpServer server;
    private final ExecutorService executor = Executors.newVirtualThreadPerTaskExecutor();
    private final AtomicReference<DelayedResponse> comparisonDelay = new AtomicReference<>();
    private final AtomicReference<DelayedResponse> passportDelay = new AtomicReference<>();
    private volatile Map<String, Object> draft;
    private static final String DRAFTS = "/api/review/drafts";
    private static final String COMPARISON = "/api/comparisons";
    private static final Map<String, String> RELEASE_REF =
            Map.of("publicationId", "publication-synthetic", "versionId", VERSION);

    LabFixture(boolean rejectSource) throws IOException {
        this.rejectSource = rejectSource;
        if (!Files.isRegularFile(assets.resolve("index.html"))) throw new IOException("React build missing: " + assets);
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.setExecutor(executor);
        server.createContext("/", this::handle);
        server.start();
    }

    URI origin() { return URI.create("http://127.0.0.1:" + server.getAddress().getPort()); }

    List<String> requests() { return requests.stream().map(Request::route).toList(); }

    String requestLog() {
        return String.join("\n", requests.stream().map(request -> request.route() + " → " + request.status()).toList());
    }

    void failNextPdf() { nextPdfFails.set(true); }
    void failNextPassport() { nextPassportFails.set(true); }
    void failNextRecommendation() { nextRecommendationFails.set(true); }

    void hideRecommendations() { emptyRecommendations = true; }

    DelayedResponse delayNextComparison() { return delayNext(comparisonDelay); }
    DelayedResponse delayNextPassport() { return delayNext(passportDelay); }

    private static DelayedResponse delayNext(AtomicReference<DelayedResponse> target) {
        var delay = new DelayedResponse();
        target.set(delay);
        return delay;
    }

    static final class DelayedResponse {
        private final CountDownLatch requested = new CountDownLatch(1);
        private final CountDownLatch released = new CountDownLatch(1);

        boolean awaitRequest() throws InterruptedException {
            return requested.await(5, TimeUnit.SECONDS);
        }
        void release() { released.countDown(); }
        void block() throws IOException {
            requested.countDown();
            try {
                if (!released.await(15, TimeUnit.SECONDS)) throw new IOException("QA response was not released");
            } catch (InterruptedException stop) {
                Thread.currentThread().interrupt();
                throw new IOException(stop);
            }
        }
    }

    private static void waitForRelease(AtomicReference<DelayedResponse> target) throws IOException {
        var delay = target.getAndSet(null);
        if (delay != null) delay.block();
    }

    byte[] inputPdf() { return pdf.clone(); }

    private byte[] resource(String name) throws IOException {
        try (var stream = LabFixture.class.getResourceAsStream(name)) {
            if (stream == null) throw new IOException("Missing QA fixture " + name);
            return stream.readAllBytes();
        }
    }

    private void handle(HttpExchange exchange) throws IOException {
        String method = exchange.getRequestMethod();
        var uri = exchange.getRequestURI();
        String route = method + " " + uri.getPath();
        try (exchange) {
            switch (route) {
                case "GET /api/articles" -> respond(exchange, 200, Map.of("articles", List.of(
                        Map.of("id", ARTICLE, "document", "synthetic.pdf", "title", "Синтетическая статья", "pageCount", 2,
                                "status", "Результаты опубликованы", "runId", "run-published", "resultUrl", version.get("url")),
                        Map.of("id", UPLOADED, "document", "another.pdf", "title", "Другая статья", "pageCount", 2,
                                "status", "Карта готова")), "unavailable", 0));
                case "POST /api/articles" -> {
                    byte[] body = exchange.getRequestBody().readAllBytes();
                    if (!"application/pdf".equals(exchange.getRequestHeaders().getFirst("Content-Type"))
                            || !"upload.pdf".equals(exchange.getRequestHeaders().getFirst("X-PDF-Name"))
                            || !java.util.Arrays.equals(body, pdf)) {
                        respond(exchange, 400, Map.of("error", "Ожидался тестовый PDF."));
                    } else respond(exchange, 201, article(UPLOADED, "upload.pdf"));
                }
                case "GET /api/articles/" + ARTICLE -> respond(exchange, 200, article(ARTICLE, "synthetic.pdf"));
                case "GET /api/articles/" + ARTICLE + "/overview-status" ->
                    respond(exchange, 200, Map.of("runId", "run-published"));
                case "GET /api/articles/" + UPLOADED + "/overview-status" ->
                    respond(exchange, 200, Map.of("status", "ready"));
                case "GET /api/articles/" + ARTICLE + "/source.pdf" ->
                    send(exchange, 200, "application/pdf", pdf);
                case "GET /api/articles/" + UPLOADED -> respond(exchange, 200, article(UPLOADED, "upload.pdf"));
                case "POST /api/articles/" + UPLOADED + "/runs" -> {
                    exchange.getRequestBody().readAllBytes();
                    if (starts.incrementAndGet() == 1) respond(exchange, 503, Map.of("error", START_ERROR));
                    else respond(exchange, 202, Map.of("id", NEW_RUN, "status", "queued"));
                }
                case "GET /api/article-runs/run-published" -> respond(exchange, 200, Map.of(
                        "id", "run-published", "articleId", ARTICLE, "publicationStatus", "done",
                        "publication", version, "stages", List.of(), "figures", List.of()));
                case "GET /api/article-runs/run-missing" -> {
                    var missing = new HashMap<String, Object>();
                    version.forEach((key, value) -> missing.put((String) key, value));
                    missing.put("versionId", "version-" + "b".repeat(40));
                    respond(exchange, 200, Map.of("id", "run-missing", "articleId", ARTICLE,
                            "publicationStatus", "done", "publication", missing, "stages", List.of(), "figures", List.of()));
                }
                case "GET /api/article-runs/" + NEW_RUN -> respond(exchange, 200, Map.of(
                        "id", NEW_RUN, "articleId", UPLOADED, "publicationStatus", "queued",
                        "stages", List.of(), "figures", List.of()));
                case "GET /trpc/publications.review" -> {
                    String input = URLDecoder.decode(uri.getRawQuery() == null ? "" : uri.getRawQuery(), StandardCharsets.UTF_8);
                    if (!input.startsWith("input=")
                            || !Map.of("publicationId", "publication-synthetic", "versionId", VERSION)
                            .equals(json.toType(input.substring(6), Map.class))) {
                        respond(exchange, 404, Map.of("error", "Выпуск не найден."));
                    } else respond(exchange, 200, Map.of("result", Map.of("data", publication)));
                }
                case "GET " + DRAFTS -> {
                    var address = query(uri);
                    boolean matches = draft != null && draft.entrySet().containsAll(address.entrySet())
                            && java.util.Objects.equals(draft.get("resultId"), address.get("resultId"));
                    respond(exchange, 200, Map.of("drafts", matches ? List.of(draft) : List.of()));
                }
                case "POST " + DRAFTS -> saveDraft(exchange);
                case "DELETE " + DRAFTS + "/draft-1" -> {
                    if (draft == null || !(readJson(exchange).get("expectedRevision") instanceof Number revision)
                            || revision.intValue() != 1) {
                        unknown(exchange);
                        return;
                    }
                    draft = null;
                    requests.add(new Request(route, 204));
                    exchange.sendResponseHeaders(204, -1);
                }
                case "POST " + COMPARISON + "/recommendations" -> {
                    if (!readJson(exchange).equals(RELEASE_REF)) { unknown(exchange); return; }
                    if (nextRecommendationFails.getAndSet(false)) {
                        respond(exchange, 503, Map.of("error", "Подбор временно недоступен."));
                        return;
                    }
                    respond(exchange, 200, Map.of("recommendations", emptyRecommendations ? List.of()
                            : List.of(recommendation("R2"), recommendation("R1"))));
                }
                case "POST " + COMPARISON + "/candidates" -> {
                    var expected = ref("R2");
                    expected.put("scope", "article");
                    var body = readJson(exchange);
                    expected.put("scope", "corpus");
                    if (body.equals(expected)) {
                        respond(exchange, 200, Map.of("candidates", List.of()));
                        return;
                    }
                    expected.put("scope", "article");
                    if (!body.equals(expected)) { unknown(exchange); return; }
                    respond(exchange, 200, Map.of("candidates", List.of(Map.of("ref", ref("R1"),
                            "status", "insufficient", "title", "Синтетическая статья", "method", "Alpha",
                            "rawValue", ".422", "reasons", List.of("Общая выборка не подтверждена.")))));
                }
                case "POST " + COMPARISON -> {
                    waitForRelease(comparisonDelay);
                    compare(exchange);
                }
                case "GET " + RELEASE + "/PASSPORT.md" -> {
                    waitForRelease(passportDelay);
                    if (nextPassportFails.getAndSet(false)) respond(exchange, 404, Map.of("error", "Паспорт недоступен."));
                    else send(exchange, 200, "text/markdown; charset=utf-8", passport().getBytes(StandardCharsets.UTF_8));
                }
                case "GET " + RELEASE + "/export.zip" -> {
                    exchange.getResponseHeaders().set("Content-Disposition", "attachment; filename=export.zip");
                    send(exchange, 200, "application/zip", export());
                }
                default -> {
                    if (method.equals("GET") && uri.getPath().equals(PDF_PATH)) {
                        if (rejectSource || nextPdfFails.getAndSet(false)) respond(exchange, 503, Map.of("error", "Управляемый отказ источника."));
                        else send(exchange, 200, "application/pdf", pdf);
                    } else if (method.equals("GET")) serveAsset(exchange, uri.getPath());
                    else respond(exchange, 404, Map.of("error", "Тестовый маршрут не задан."));
                }
            }
        }
    }

    private Map<String, String> ref(String result) {
        var ref = new HashMap<>(RELEASE_REF);
        ref.put("resultId", result);
        return ref;
    }

    private Map<String, Object> recommendation(String first) {
        return Map.of("a", ref(first), "b", ref("R4"), "methods", List.of("Alpha", "Beta"),
                "dataset", "HotpotQA", "metric", "EM", "delta", "0.078", "unit", "fraction");
    }

    private Map<String, String> query(URI uri) {
        var fields = new HashMap<String, String>();
        if (uri.getRawQuery() != null) for (String entry : uri.getRawQuery().split("&")) {
            String[] pair = entry.split("=", 2);
            fields.put(pair[0], URLDecoder.decode(pair.length == 2 ? pair[1] : "", StandardCharsets.UTF_8));
        }
        return fields;
    }

    private Map<?, ?> readJson(HttpExchange exchange) throws IOException {
        return json.toType(new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8), Map.class);
    }

    private Map<?, ?> record(String list, String id) {
        return ((List<?>) publication.get(list)).stream().map(Map.class::cast)
                .filter(item -> id.equals(item.get("id"))).findFirst().orElseThrow();
    }

    private void saveDraft(HttpExchange exchange) throws IOException {
        var body = readJson(exchange);
        if (!body.entrySet().containsAll(RELEASE_REF.entrySet()) || !sha.equals(body.get("pdfSha256"))
                || !(body.get("reason") instanceof String reason) || reason.isBlank()) {
            unknown(exchange);
            return;
        }
        Map<String, Object> entry = new HashMap<>(RELEASE_REF);
        entry.putAll(Map.of("id", "draft-1", "revision", 1, "reason", reason));
        if ("I2".equals(body.get("remarkId")) && "leave-open".equals(body.get("disposition"))
                && List.of().equals(body.get("evidenceIds"))) {
            entry.putAll(Map.of("remarkId", "I2", "disposition", "leave-open", "evidence", List.of()));
        } else if ("I1".equals(body.get("remarkId")) && "R2".equals(body.get("resultId"))
                && "proposal".equals(body.get("disposition")) && List.of("S2").equals(body.get("evidenceIds"))
                && List.of("value", "unit").contains(body.get("field")) && body.get("proposedValue") instanceof String value) {
            entry.putAll(Map.of("remarkId", "I1", "resultId", "R2", "field", body.get("field"),
                    "disposition", "proposal", "originalValue", "unit".equals(body.get("field")) ? "fraction" : "0.422",
                    "proposedValue", value, "evidence", List.of(record("evidence", "S2"))));
        } else {
            unknown(exchange);
            return;
        }
        draft = entry;
        respond(exchange, 201, draft);
    }

    private Map<String, Object> side(String result, String evidence) {
        return Map.of("ref", ref(result), "result", record("results", result), "evidence",
                List.of(record("evidence", evidence)), "pdfUrl", PDF_PATH, "title", "Синтетическая статья");
    }

    private void compare(HttpExchange exchange) throws IOException {
        var body = readJson(exchange);
        String left = ref("R2").equals(body.get("a")) ? "R2" : ref("R1").equals(body.get("a")) ? "R1" : null;
        String right = List.of("R1", "R4", "R5").stream().filter(id -> ref(id).equals(body.get("b"))).findFirst().orElse(null);
        if (left == null || right == null || left.equals(right)) {
            respond(exchange, 404, Map.of("error", "Запись не найдена в закреплённой версии."));
            return;
        }
        String leftSource = "S" + left.substring(1);
        boolean comparable = right.equals("R4");
        boolean different = right.equals("R5");
        var decision = new HashMap<String, Object>();
        decision.putAll(Map.of("a", side(left, leftSource), "b", side(right, "S" + right.substring(1)),
                "checks", List.of(), "status", comparable ? "comparable" : different ? "different" : "insufficient", "reasons",
                List.of(comparable ? "Подтверждён общий протокол." : different ? "Метрики различаются." : "Общая выборка не подтверждена.")));
        if (!comparable) decision.put("delta", "999");
        if (comparable) {
            var source = new HashMap<>(RELEASE_REF);
            source.put("evidenceId", leftSource);
            decision.putAll(Map.of("delta", "0.078", "unit", "fraction", "proof", Map.of("source", source)));
        }
        respond(exchange, 200, decision);
    }

    private String passport() {
        return "# Синтетический паспорт\nВерсия " + VERSION + "\n\n" + """
                ## Контрольная запись
                [Запись R2](?resultId=R2)
                ## Связи и типы
                Результат связан с экспериментом.
                ## Поля в CSV
                Значение, метрика и набор.
                ## Охват и сведения о версии
                Выпуск содержит контрольные таблицы.
                """;
    }

    private byte[] export() throws IOException {
        var output = new ByteArrayOutputStream();
        try (var archive = new ZipOutputStream(output)) {
            for (String name : List.of("publications", "configurations", "datasets", "tasks",
                    "experiments", "results", "issues")) {
                zipEntry(archive, name + ".csv", "publication_id\npublication-synthetic\n");
            }
            zipEntry(archive, "PASSPORT.md", passport());
            zipEntry(archive, "report.json", "{\"exit_code\":0}");
            zipEntry(archive, "manifest.json", json.toJson(Map.of("versionId", VERSION, "runId", "run-published")));
        }
        return output.toByteArray();
    }

    private void zipEntry(ZipOutputStream archive, String name, String content) throws IOException {
        archive.putNextEntry(new ZipEntry(name));
        archive.write(content.getBytes(StandardCharsets.UTF_8));
        archive.closeEntry();
    }

    private void unknown(HttpExchange exchange) throws IOException {
        respond(exchange, 404, Map.of("error", "Тестовый адрес не распознан."));
    }

    private Map<String, Object> article(String id, String file) {
        return Map.of("id", id, "document", file, "sha256", sha,
                "page_count", 2, "candidates", id.equals(ARTICLE) ? List.of(
                        Map.of("id", "section", "kind", "section", "text", "Методика", "page", 1),
                        Map.of("id", "table", "kind", "table", "text", "Table 1", "page", 1, "box", List.of(.35, .065, .51, .12)),
                        Map.of("id", "figure", "kind", "figure", "text", "Figure 3", "page", 2, "box", List.of(.35, .065, .51, .12))
                ) : List.of(), "warnings", List.of());
    }

    private void serveAsset(HttpExchange exchange, String path) throws IOException {
        String name = path.equals("/") ? "index.html" : path.substring(1);
        Path file = assets.resolve(name).normalize();
        if ((!name.equals("index.html") && !name.startsWith("assets/"))
                || !file.startsWith(assets) || !Files.isRegularFile(file)) {
            respond(exchange, 404, Map.of("error", "Тестовый маршрут не задан."));
            return;
        }
        String type = name.endsWith(".css") ? "text/css" : name.endsWith(".html") ? "text/html" : "text/javascript";
        send(exchange, 200, type + "; charset=utf-8", Files.readAllBytes(file));
    }

    private void respond(HttpExchange exchange, int status, Object body) throws IOException {
        send(exchange, status, "application/json; charset=utf-8", json.toJson(body).getBytes(StandardCharsets.UTF_8));
    }

    private void send(HttpExchange exchange, int status, String contentType, byte[] body) throws IOException {
        requests.add(new Request(exchange.getRequestMethod() + " " + exchange.getRequestURI().getPath(), status));
        exchange.getResponseHeaders().set("Content-Type", contentType);
        exchange.getResponseHeaders().set("Cache-Control", "no-store");
        exchange.sendResponseHeaders(status, body.length);
        exchange.getResponseBody().write(body);
    }

    @Override public void close() {
        server.stop(0);
        executor.shutdownNow();
    }
}
