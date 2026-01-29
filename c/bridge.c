#define _GNU_SOURCE
#include <arpa/inet.h>
#include <ctype.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <microhttpd.h>
#include <pthread.h>
#include <signal.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#include <curl/curl.h>
#include <cjson/cJSON.h>

static const char *VERSION = "0.9.5";
static const char *PERSISTENCE_NOTE = "They'll stay on your team.";
static const char *IMAGE_INBOX_ROOT = "/tmp/claudecode-telegram";
static const size_t MAX_IMAGE_SIZE = 20 * 1024 * 1024;

static const char *ALLOWED_IMAGE_EXTENSIONS[] = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", NULL
};

static const char *BOT_COMMANDS[][2] = {
    {"team", "Show your team"},
    {"focus", "Focus a worker: /focus <name>"},
    {"progress", "Check focused worker status"},
    {"learn", "Ask focused worker what they learned"},
    {"pause", "Pause focused worker"},
    {"relaunch", "Relaunch focused worker"},
    {"settings", "Show settings"},
    {"hire", "Hire a worker: /hire <name>"},
    {"end", "Offboard a worker: /end <name>"},
    {NULL, NULL}
};

static const char *BLOCKED_COMMANDS[] = {
    "/mcp", "/help", "/config", "/model", "/compact", "/cost",
    "/doctor", "/init", "/login", "/logout", "/memory", "/permissions",
    "/pr", "/review", "/terminal", "/vim", "/approved-tools", "/listen",
    NULL
};

static const char *RESERVED_NAMES[] = {
    "team", "focus", "progress", "learn", "pause", "relaunch", "settings", "hire", "end",
    "new", "use", "list", "kill", "status", "stop", "restart", "system",
    "all", "start", "help",
    NULL
};

static char *BOT_TOKEN = NULL;
static char *WEBHOOK_SECRET = NULL;
static char *SESSIONS_DIR = NULL;
static char *TMUX_PREFIX = NULL;
static const char *TMUX_BIN = "tmux";
static int PORT = 8080;

struct State {
    char *active;
    char *pending_registration;
    int startup_notified;
};

static struct State state = {0};
static long long admin_chat_id = 0;
static int admin_known = 0;

static volatile sig_atomic_t shutdown_requested = 0;
static struct MHD_Daemon *server_daemon = NULL;

struct strlist {
    char **items;
    size_t len;
    size_t cap;
};

struct session_entry {
    char *name;
    char *tmux;
};

struct session_list {
    struct session_entry *items;
    size_t len;
    size_t cap;
};

struct session_lock {
    char *name;
    pthread_mutex_t lock;
    struct session_lock *next;
};

static struct session_lock *lock_head = NULL;
static pthread_mutex_t lock_guard = PTHREAD_MUTEX_INITIALIZER;

struct buffer {
    char *data;
    size_t len;
};

struct image_tag {
    char *path;
    char *caption;
};

struct image_list {
    struct image_tag *items;
    size_t len;
    size_t cap;
};

static void *xmalloc(size_t n) {
    void *p = malloc(n);
    if (!p) {
        fprintf(stderr, "Out of memory\n");
        exit(1);
    }
    return p;
}

static char *xstrdup(const char *s) {
    if (!s) return NULL;
    size_t n = strlen(s) + 1;
    char *p = xmalloc(n);
    memcpy(p, s, n);
    return p;
}

static int starts_with(const char *s, const char *prefix) {
    return strncmp(s, prefix, strlen(prefix)) == 0;
}

static int ends_with(const char *s, const char *suffix) {
    size_t slen = strlen(s);
    size_t suflen = strlen(suffix);
    if (slen < suflen) return 0;
    return strcmp(s + slen - suflen, suffix) == 0;
}

static void strlist_add(struct strlist *list, const char *s) {
    if (list->len + 1 > list->cap) {
        size_t new_cap = list->cap ? list->cap * 2 : 8;
        list->items = realloc(list->items, new_cap * sizeof(char *));
        list->cap = new_cap;
    }
    list->items[list->len++] = xstrdup(s);
}

static int strlist_contains(struct strlist *list, const char *s) {
    for (size_t i = 0; i < list->len; i++) {
        if (strcmp(list->items[i], s) == 0) return 1;
    }
    return 0;
}

static void strlist_add_unique(struct strlist *list, const char *s) {
    if (!strlist_contains(list, s)) strlist_add(list, s);
}

static void strlist_free(struct strlist *list) {
    for (size_t i = 0; i < list->len; i++) {
        free(list->items[i]);
    }
    free(list->items);
    list->items = NULL;
    list->len = list->cap = 0;
}

static void session_list_add(struct session_list *list, const char *name, const char *tmux) {
    if (list->len + 1 > list->cap) {
        size_t new_cap = list->cap ? list->cap * 2 : 8;
        list->items = realloc(list->items, new_cap * sizeof(struct session_entry));
        list->cap = new_cap;
    }
    list->items[list->len].name = xstrdup(name);
    list->items[list->len].tmux = xstrdup(tmux);
    list->len++;
}

static void session_list_free(struct session_list *list) {
    for (size_t i = 0; i < list->len; i++) {
        free(list->items[i].name);
        free(list->items[i].tmux);
    }
    free(list->items);
    list->items = NULL;
    list->len = list->cap = 0;
}

static struct session_entry *session_list_find(struct session_list *list, const char *name) {
    for (size_t i = 0; i < list->len; i++) {
        if (strcmp(list->items[i].name, name) == 0) {
            return &list->items[i];
        }
    }
    return NULL;
}

static int session_entry_cmp(const void *a, const void *b) {
    const struct session_entry *ea = (const struct session_entry *)a;
    const struct session_entry *eb = (const struct session_entry *)b;
    return strcmp(ea->name, eb->name);
}

static void session_list_sort(struct session_list *list) {
    if (list->len > 1) {
        qsort(list->items, list->len, sizeof(struct session_entry), session_entry_cmp);
    }
}

static pthread_mutex_t *get_session_lock(const char *name) {
    pthread_mutex_lock(&lock_guard);
    struct session_lock *cur = lock_head;
    while (cur) {
        if (strcmp(cur->name, name) == 0) {
            pthread_mutex_unlock(&lock_guard);
            return &cur->lock;
        }
        cur = cur->next;
    }
    struct session_lock *node = xmalloc(sizeof(*node));
    node->name = xstrdup(name);
    pthread_mutex_init(&node->lock, NULL);
    node->next = lock_head;
    lock_head = node;
    pthread_mutex_unlock(&lock_guard);
    return &node->lock;
}

static void image_list_add(struct image_list *list, const char *path, const char *caption) {
    if (list->len + 1 > list->cap) {
        size_t new_cap = list->cap ? list->cap * 2 : 4;
        list->items = realloc(list->items, new_cap * sizeof(struct image_tag));
        list->cap = new_cap;
    }
    list->items[list->len].path = xstrdup(path);
    list->items[list->len].caption = xstrdup(caption ? caption : "");
    list->len++;
}

static void image_list_free(struct image_list *list) {
    for (size_t i = 0; i < list->len; i++) {
        free(list->items[i].path);
        free(list->items[i].caption);
    }
    free(list->items);
    list->items = NULL;
    list->len = list->cap = 0;
}

static char *str_tolower_copy(const char *s) {
    size_t n = strlen(s);
    char *out = xmalloc(n + 1);
    for (size_t i = 0; i < n; i++) {
        out[i] = (char)tolower((unsigned char)s[i]);
    }
    out[n] = '\0';
    return out;
}

static char *sanitize_name(const char *s) {
    size_t n = strlen(s);
    char *out = xmalloc(n + 1);
    size_t j = 0;
    for (size_t i = 0; i < n; i++) {
        char c = (char)tolower((unsigned char)s[i]);
        if ((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '-') {
            out[j++] = c;
        }
    }
    out[j] = '\0';
    return out;
}

static int is_reserved_name(const char *name) {
    for (int i = 0; RESERVED_NAMES[i]; i++) {
        if (strcmp(RESERVED_NAMES[i], name) == 0) return 1;
    }
    return 0;
}

static int is_blocked_command(const char *cmd) {
    for (int i = 0; BLOCKED_COMMANDS[i]; i++) {
        if (strcmp(BLOCKED_COMMANDS[i], cmd) == 0) return 1;
    }
    return 0;
}

static int mkdir_p(const char *path, mode_t mode) {
    char tmp[PATH_MAX];
    snprintf(tmp, sizeof(tmp), "%s", path);
    size_t len = strlen(tmp);
    if (len == 0) return -1;
    if (tmp[len - 1] == '/') tmp[len - 1] = '\0';

    for (char *p = tmp + 1; *p; p++) {
        if (*p == '/') {
            *p = '\0';
            mkdir(tmp, mode);
            *p = '/';
        }
    }
    if (mkdir(tmp, mode) != 0 && errno != EEXIST) return -1;
    return 0;
}

static int write_text_file(const char *path, const char *text, mode_t mode) {
    FILE *f = fopen(path, "w");
    if (!f) return -1;
    fputs(text, f);
    fclose(f);
    chmod(path, mode);
    return 0;
}

static char *read_text_file(const char *path) {
    FILE *f = fopen(path, "r");
    if (!f) return NULL;
    fseek(f, 0, SEEK_END);
    long size = ftell(f);
    fseek(f, 0, SEEK_SET);
    if (size < 0) { fclose(f); return NULL; }
    char *buf = xmalloc((size_t)size + 1);
    if (fread(buf, 1, (size_t)size, f) != (size_t)size) {
        fclose(f);
        free(buf);
        return NULL;
    }
    buf[size] = '\0';
    fclose(f);
    return buf;
}

static int file_exists(const char *path) {
    struct stat st;
    return stat(path, &st) == 0;
}

static char *path_join(const char *a, const char *b) {
    size_t n = strlen(a) + strlen(b) + 2;
    char *out = xmalloc(n);
    snprintf(out, n, "%s/%s", a, b);
    return out;
}

static char *get_session_dir(const char *name) {
    return path_join(SESSIONS_DIR, name);
}

static char *get_pending_file(const char *name) {
    char *dir = get_session_dir(name);
    char *path = path_join(dir, "pending");
    free(dir);
    return path;
}

static char *get_chat_id_file(const char *name) {
    char *dir = get_session_dir(name);
    char *path = path_join(dir, "chat_id");
    free(dir);
    return path;
}

static int ensure_session_dir(const char *name) {
    char *dir = get_session_dir(name);
    int rc = mkdir_p(dir, 0700);
    if (rc == 0) {
        chmod(SESSIONS_DIR, 0700);
        chmod(dir, 0700);
    }
    free(dir);
    return rc;
}

static void set_pending(const char *name, long long chat_id) {
    if (ensure_session_dir(name) != 0) return;
    char ts[32];
    snprintf(ts, sizeof(ts), "%ld", (long)time(NULL));
    char *pending = get_pending_file(name);
    char *chat = get_chat_id_file(name);
    write_text_file(pending, ts, 0600);
    char chatbuf[32];
    snprintf(chatbuf, sizeof(chatbuf), "%lld", chat_id);
    write_text_file(chat, chatbuf, 0600);
    free(pending);
    free(chat);
}

static void clear_pending(const char *name) {
    char *pending = get_pending_file(name);
    unlink(pending);
    free(pending);
}

static int is_pending(const char *name) {
    char *pending = get_pending_file(name);
    if (!file_exists(pending)) {
        free(pending);
        return 0;
    }
    char *txt = read_text_file(pending);
    if (!txt) {
        free(pending);
        return 0;
    }
    long ts = atol(txt);
    free(txt);
    if (time(NULL) - ts > 600) {
        unlink(pending);
        free(pending);
        return 0;
    }
    free(pending);
    return 1;
}

static char *get_inbox_dir(const char *session_name) {
    char *root = path_join(IMAGE_INBOX_ROOT, session_name);
    char *inbox = path_join(root, "inbox");
    free(root);
    return inbox;
}

static void cleanup_inbox(const char *session_name) {
    char *inbox = get_inbox_dir(session_name);
    DIR *dir = opendir(inbox);
    if (dir) {
        struct dirent *ent;
        while ((ent = readdir(dir)) != NULL) {
            if (strcmp(ent->d_name, ".") == 0 || strcmp(ent->d_name, "..") == 0) continue;
            char *path = path_join(inbox, ent->d_name);
            unlink(path);
            free(path);
        }
        closedir(dir);
    }
    free(inbox);
}

static int ensure_inbox_dir(const char *session_name, char *out_path, size_t out_len) {
    char *inbox = get_inbox_dir(session_name);
    if (mkdir_p(inbox, 0700) != 0) {
        free(inbox);
        return -1;
    }
    chmod(inbox, 0700);
    snprintf(out_path, out_len, "%s", inbox);
    free(inbox);
    return 0;
}

static void random_hex(char *out, size_t len) {
    size_t bytes = len / 2;
    unsigned char *buf = xmalloc(bytes);
    int fd = open("/dev/urandom", O_RDONLY);
    if (fd >= 0) {
        if (read(fd, buf, bytes) != (ssize_t)bytes) {
            close(fd);
            fd = -1;
        } else {
            close(fd);
        }
    }
    if (fd < 0) {
        srand((unsigned int)time(NULL));
        for (size_t i = 0; i < bytes; i++) buf[i] = (unsigned char)(rand() & 0xFF);
    }
    for (size_t i = 0; i < bytes; i++) {
        snprintf(out + (i * 2), 3, "%02x", buf[i]);
    }
    out[len] = '\0';
    free(buf);
}

static int has_allowed_extension(const char *path) {
    char *lower = str_tolower_copy(path);
    int ok = 0;
    for (int i = 0; ALLOWED_IMAGE_EXTENSIONS[i]; i++) {
        if (ends_with(lower, ALLOWED_IMAGE_EXTENSIONS[i])) { ok = 1; break; }
    }
    free(lower);
    return ok;
}

static int path_has_prefix(const char *path, const char *root) {
    size_t len = strlen(root);
    if (strncmp(path, root, len) != 0) return 0;
    return path[len] == '/' || path[len] == '\0';
}

static int is_path_allowed(const char *path) {
    char resolved[PATH_MAX];
    if (!realpath(path, resolved)) return 0;
    char tmp_root[PATH_MAX];
    char sess_root[PATH_MAX];
    char cwd_root[PATH_MAX];
    if (!realpath("/tmp", tmp_root)) tmp_root[0] = '\0';
    if (!realpath(SESSIONS_DIR, sess_root)) sess_root[0] = '\0';
    if (!getcwd(cwd_root, sizeof(cwd_root))) cwd_root[0] = '\0';
    if (tmp_root[0] && path_has_prefix(resolved, tmp_root)) return 1;
    if (sess_root[0] && path_has_prefix(resolved, sess_root)) return 1;
    if (cwd_root[0] && path_has_prefix(resolved, cwd_root)) return 1;
    return 0;
}

static size_t write_cb(void *contents, size_t size, size_t nmemb, void *userp) {
    size_t realsize = size * nmemb;
    struct buffer *mem = (struct buffer *)userp;
    char *ptr = realloc(mem->data, mem->len + realsize + 1);
    if (!ptr) return 0;
    mem->data = ptr;
    memcpy(&(mem->data[mem->len]), contents, realsize);
    mem->len += realsize;
    mem->data[mem->len] = '\0';
    return realsize;
}

static cJSON *telegram_api_json(const char *method, cJSON *payload) {
    if (!BOT_TOKEN || !*BOT_TOKEN) return NULL;
    CURL *curl = curl_easy_init();
    if (!curl) return NULL;

    char url[512];
    snprintf(url, sizeof(url), "https://api.telegram.org/bot%s/%s", BOT_TOKEN, method);
    char *json_str = cJSON_PrintUnformatted(payload);

    struct buffer chunk = {0};
    struct curl_slist *headers = NULL;
    headers = curl_slist_append(headers, "Content-Type: application/json");

    curl_easy_setopt(curl, CURLOPT_URL, url);
    curl_easy_setopt(curl, CURLOPT_POSTFIELDS, json_str);
    curl_easy_setopt(curl, CURLOPT_HTTPHEADER, headers);
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, write_cb);
    curl_easy_setopt(curl, CURLOPT_WRITEDATA, (void *)&chunk);
    curl_easy_setopt(curl, CURLOPT_TIMEOUT, 15L);

    CURLcode res = curl_easy_perform(curl);
    curl_slist_free_all(headers);
    curl_easy_cleanup(curl);
    free(json_str);

    if (res != CURLE_OK) {
        free(chunk.data);
        return NULL;
    }
    cJSON *resp = cJSON_Parse(chunk.data);
    free(chunk.data);
    return resp;
}

static int telegram_send_message(long long chat_id, const char *text, const char *parse_mode) {
    cJSON *payload = cJSON_CreateObject();
    cJSON_AddNumberToObject(payload, "chat_id", (double)chat_id);
    cJSON_AddStringToObject(payload, "text", text);
    if (parse_mode) cJSON_AddStringToObject(payload, "parse_mode", parse_mode);
    cJSON *resp = telegram_api_json("sendMessage", payload);
    cJSON_Delete(payload);
    if (!resp) return 0;
    cJSON *ok = cJSON_GetObjectItem(resp, "ok");
    int success = cJSON_IsTrue(ok);
    cJSON_Delete(resp);
    return success;
}

static int telegram_set_reaction(long long chat_id, long long msg_id) {
    cJSON *payload = cJSON_CreateObject();
    cJSON_AddNumberToObject(payload, "chat_id", (double)chat_id);
    cJSON_AddNumberToObject(payload, "message_id", (double)msg_id);
    cJSON *arr = cJSON_AddArrayToObject(payload, "reaction");
    cJSON *obj = cJSON_CreateObject();
    cJSON_AddStringToObject(obj, "type", "emoji");
    cJSON_AddStringToObject(obj, "emoji", "\xF0\x9F\x91\x80");
    cJSON_AddItemToArray(arr, obj);
    cJSON *resp = telegram_api_json("setMessageReaction", payload);
    cJSON_Delete(payload);
    if (!resp) return 0;
    cJSON *ok = cJSON_GetObjectItem(resp, "ok");
    int success = cJSON_IsTrue(ok);
    cJSON_Delete(resp);
    return success;
}

static void telegram_send_chat_action(long long chat_id) {
    cJSON *payload = cJSON_CreateObject();
    cJSON_AddNumberToObject(payload, "chat_id", (double)chat_id);
    cJSON_AddStringToObject(payload, "action", "typing");
    cJSON *resp = telegram_api_json("sendChatAction", payload);
    cJSON_Delete(payload);
    if (resp) cJSON_Delete(resp);
}

static int telegram_set_commands(struct session_list *registered) {
    cJSON *payload = cJSON_CreateObject();
    cJSON *arr = cJSON_AddArrayToObject(payload, "commands");

    for (int i = 0; BOT_COMMANDS[i][0]; i++) {
        cJSON *cmd = cJSON_CreateObject();
        cJSON_AddStringToObject(cmd, "command", BOT_COMMANDS[i][0]);
        cJSON_AddStringToObject(cmd, "description", BOT_COMMANDS[i][1]);
        cJSON_AddItemToArray(arr, cmd);
    }

    for (size_t i = 0; i < registered->len; i++) {
        cJSON *cmd = cJSON_CreateObject();
        cJSON_AddStringToObject(cmd, "command", registered->items[i].name);
        char desc[128];
        snprintf(desc, sizeof(desc), "Message %s", registered->items[i].name);
        cJSON_AddStringToObject(cmd, "description", desc);
        cJSON_AddItemToArray(arr, cmd);
    }

    cJSON *resp = telegram_api_json("setMyCommands", payload);
    cJSON_Delete(payload);
    if (!resp) return 0;
    cJSON *ok = cJSON_GetObjectItem(resp, "ok");
    int success = cJSON_IsTrue(ok);
    cJSON_Delete(resp);
    return success;
}

static int send_photo(long long chat_id, const char *photo_path, const char *caption) {
    if (!BOT_TOKEN || !*BOT_TOKEN) return 0;
    if (!file_exists(photo_path)) return 0;
    if (!has_allowed_extension(photo_path)) return 0;
    if (!is_path_allowed(photo_path)) return 0;

    struct stat st;
    if (stat(photo_path, &st) != 0) return 0;
    if ((size_t)st.st_size > MAX_IMAGE_SIZE) return 0;

    CURL *curl = curl_easy_init();
    if (!curl) return 0;

    char url[512];
    snprintf(url, sizeof(url), "https://api.telegram.org/bot%s/sendPhoto", BOT_TOKEN);
    curl_easy_setopt(curl, CURLOPT_URL, url);
    curl_easy_setopt(curl, CURLOPT_TIMEOUT, 60L);

    curl_mime *mime = curl_mime_init(curl);
    curl_mimepart *part = curl_mime_addpart(mime);
    char chatbuf[32];
    snprintf(chatbuf, sizeof(chatbuf), "%lld", chat_id);
    curl_mime_name(part, "chat_id");
    curl_mime_data(part, chatbuf, CURL_ZERO_TERMINATED);

    part = curl_mime_addpart(mime);
    curl_mime_name(part, "photo");
    curl_mime_filedata(part, photo_path);

    if (caption && *caption) {
        part = curl_mime_addpart(mime);
        curl_mime_name(part, "caption");
        curl_mime_data(part, caption, CURL_ZERO_TERMINATED);
    }

    curl_easy_setopt(curl, CURLOPT_MIMEPOST, mime);

    struct buffer chunk = {0};
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, write_cb);
    curl_easy_setopt(curl, CURLOPT_WRITEDATA, (void *)&chunk);

    CURLcode res = curl_easy_perform(curl);
    int ok = 0;
    if (res == CURLE_OK) {
        cJSON *resp = cJSON_Parse(chunk.data ? chunk.data : "{}");
        if (resp) {
            cJSON *is_ok = cJSON_GetObjectItem(resp, "ok");
            ok = cJSON_IsTrue(is_ok);
            cJSON_Delete(resp);
        }
    }

    free(chunk.data);
    curl_mime_free(mime);
    curl_easy_cleanup(curl);
    return ok;
}

static char *download_telegram_file(const char *file_id, const char *session_name) {
    if (!BOT_TOKEN || !*BOT_TOKEN) return NULL;
    cJSON *payload = cJSON_CreateObject();
    cJSON_AddStringToObject(payload, "file_id", file_id);
    cJSON *resp = telegram_api_json("getFile", payload);
    cJSON_Delete(payload);
    if (!resp) return NULL;

    cJSON *ok = cJSON_GetObjectItem(resp, "ok");
    if (!cJSON_IsTrue(ok)) {
        cJSON_Delete(resp);
        return NULL;
    }
    cJSON *result = cJSON_GetObjectItem(resp, "result");
    const char *file_path = cJSON_GetObjectItem(result, "file_path") ? cJSON_GetObjectItem(result, "file_path")->valuestring : NULL;
    long long file_size = cJSON_GetObjectItem(result, "file_size") ? (long long)cJSON_GetObjectItem(result, "file_size")->valuedouble : 0;
    if (!file_path || file_size > (long long)MAX_IMAGE_SIZE) {
        cJSON_Delete(resp);
        return NULL;
    }

    char inbox[PATH_MAX];
    if (ensure_inbox_dir(session_name, inbox, sizeof(inbox)) != 0) {
        cJSON_Delete(resp);
        return NULL;
    }

    char hex[33];
    random_hex(hex, 32);
    const char *ext = strrchr(file_path, '.');
    if (!ext) ext = ".jpg";

    char local_path[PATH_MAX];
    snprintf(local_path, sizeof(local_path), "%s/%s%s", inbox, hex, ext);

    char url[1024];
    snprintf(url, sizeof(url), "https://api.telegram.org/file/bot%s/%s", BOT_TOKEN, file_path);

    FILE *f = fopen(local_path, "wb");
    if (!f) {
        cJSON_Delete(resp);
        return NULL;
    }

    CURL *curl = curl_easy_init();
    if (!curl) {
        fclose(f);
        cJSON_Delete(resp);
        return NULL;
    }

    curl_easy_setopt(curl, CURLOPT_URL, url);
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, NULL);
    curl_easy_setopt(curl, CURLOPT_WRITEDATA, f);
    curl_easy_setopt(curl, CURLOPT_TIMEOUT, 60L);

    CURLcode res = curl_easy_perform(curl);
    curl_easy_cleanup(curl);
    fclose(f);

    cJSON_Delete(resp);
    if (res != CURLE_OK) {
        unlink(local_path);
        return NULL;
    }
    struct stat st;
    if (stat(local_path, &st) != 0 || (size_t)st.st_size > MAX_IMAGE_SIZE) {
        unlink(local_path);
        return NULL;
    }
    chmod(local_path, 0600);
    return xstrdup(local_path);
}

static char *trim_whitespace(char *s) {
    char *end;
    while (isspace((unsigned char)*s)) s++;
    if (*s == 0) return s;
    end = s + strlen(s) - 1;
    while (end > s && isspace((unsigned char)*end)) end--;
    end[1] = '\0';
    return s;
}

static char *trim_copy(const char *s) {
    if (!s) return xstrdup("");
    while (isspace((unsigned char)*s)) s++;
    const char *end = s + strlen(s);
    while (end > s && isspace((unsigned char)*(end - 1))) end--;
    size_t len = (size_t)(end - s);
    char *out = xmalloc(len + 1);
    memcpy(out, s, len);
    out[len] = '\0';
    return out;
}

static char *collapse_newlines(const char *s) {
    size_t n = strlen(s);
    char *out = xmalloc(n + 1);
    size_t j = 0;
    int newline_count = 0;
    for (size_t i = 0; i < n; i++) {
        if (s[i] == '\n') {
            newline_count++;
            if (newline_count <= 2) {
                out[j++] = s[i];
            }
        } else {
            newline_count = 0;
            out[j++] = s[i];
        }
    }
    out[j] = '\0';
    return out;
}

static char *parse_image_tags(const char *text, struct image_list *images) {
    const char *p = text;
    size_t out_cap = strlen(text) + 1;
    char *out = xmalloc(out_cap);
    size_t out_len = 0;

    while (*p) {
        const char *start = strstr(p, "[[image:");
        if (!start) {
            size_t rem = strlen(p);
            memcpy(out + out_len, p, rem);
            out_len += rem;
            break;
        }
        size_t prefix_len = (size_t)(start - p);
        memcpy(out + out_len, p, prefix_len);
        out_len += prefix_len;

        const char *end = strstr(start, "]]" );
        if (!end) {
            size_t rem = strlen(start);
            memcpy(out + out_len, start, rem);
            out_len += rem;
            break;
        }

        const char *content = start + strlen("[[image:");
        size_t content_len = (size_t)(end - content);
        char *content_buf = xmalloc(content_len + 1);
        memcpy(content_buf, content, content_len);
        content_buf[content_len] = '\0';

        char *sep = strchr(content_buf, '|');
        if (sep) {
            *sep = '\0';
            char *path = trim_whitespace(content_buf);
            char *caption = trim_whitespace(sep + 1);
            if (*path) image_list_add(images, path, caption);
        } else {
            char *path = trim_whitespace(content_buf);
            if (*path) image_list_add(images, path, "");
        }

        free(content_buf);
        p = end + 2;
    }

    out[out_len] = '\0';
    char *collapsed = collapse_newlines(out);
    free(out);
    char *trimmed = trim_whitespace(collapsed);
    char *final = xstrdup(trimmed);
    free(collapsed);
    return final;
}

static char *format_response_text(const char *session_name, const char *text) {
    size_t n = strlen(session_name) + strlen(text) + 16;
    char *out = xmalloc(n);
    snprintf(out, n, "<b>%s:</b>\n%s", session_name, text);
    return out;
}

static int run_cmd_status(const char *cmd) {
    int rc = system(cmd);
    if (rc == -1) return -1;
    if (WIFEXITED(rc)) return WEXITSTATUS(rc);
    return -1;
}

static int run_tmux_args(char *const argv[]) {
    pid_t pid = fork();
    if (pid == 0) {
        execvp(TMUX_BIN, argv);
        _exit(127);
    }
    if (pid < 0) return -1;
    int status = 0;
    if (waitpid(pid, &status, 0) < 0) return -1;
    if (WIFEXITED(status)) return WEXITSTATUS(status);
    return -1;
}

static char *run_cmd_capture(const char *cmd) {
    FILE *fp = popen(cmd, "r");
    if (!fp) return NULL;
    struct buffer buf = {0};
    char tmp[256];
    while (fgets(tmp, sizeof(tmp), fp)) {
        size_t len = strlen(tmp);
        char *newp = realloc(buf.data, buf.len + len + 1);
        if (!newp) break;
        buf.data = newp;
        memcpy(buf.data + buf.len, tmp, len);
        buf.len += len;
        buf.data[buf.len] = '\0';
    }
    pclose(fp);
    return buf.data;
}

static int tmux_exists(const char *tmux_name) {
    char cmd[512];
    snprintf(cmd, sizeof(cmd), "%s has-session -t %s 2>/dev/null", TMUX_BIN, tmux_name);
    return run_cmd_status(cmd) == 0;
}

static char *get_pane_command(const char *tmux_name) {
    char cmd[512];
    snprintf(cmd, sizeof(cmd), "%s display-message -t %s -p '#{pane_current_command}'", TMUX_BIN, tmux_name);
    char *out = run_cmd_capture(cmd);
    if (!out) return xstrdup("");
    char *trim = trim_whitespace(out);
    char *res = xstrdup(trim);
    free(out);
    return res;
}

static int is_claude_running(const char *tmux_name) {
    char *cmd = get_pane_command(tmux_name);
    char *lower = str_tolower_copy(cmd);
    int ok = strstr(lower, "claude") != NULL;
    free(cmd);
    free(lower);
    return ok;
}

static int tmux_send(const char *tmux_name, const char *text, int literal) {
    if (literal) {
    char *argv[] = {(char *)TMUX_BIN, "send-keys", "-t", (char *)tmux_name, "-l", (char *)text, NULL};
        return run_tmux_args(argv) == 0;
    }
    char *argv[] = {(char *)TMUX_BIN, "send-keys", "-t", (char *)tmux_name, (char *)text, NULL};
    return run_tmux_args(argv) == 0;
}

static int tmux_send_enter(const char *tmux_name) {
    char *argv[] = {(char *)TMUX_BIN, "send-keys", "-t", (char *)tmux_name, "Enter", NULL};
    return run_tmux_args(argv) == 0;
}

static int tmux_send_message(const char *tmux_name, const char *text) {
    pthread_mutex_t *lock = get_session_lock(tmux_name);
    pthread_mutex_lock(lock);
    int ok1 = tmux_send(tmux_name, text, 1);
    int ok2 = tmux_send_enter(tmux_name);
    pthread_mutex_unlock(lock);
    return ok1 && ok2;
}

static void tmux_send_escape(const char *tmux_name) {
    char *argv[] = {(char *)TMUX_BIN, "send-keys", "-t", (char *)tmux_name, "Escape", NULL};
    run_tmux_args(argv);
}

static void export_hook_env(const char *tmux_name) {
    char env_line[1024];
    snprintf(env_line, sizeof(env_line), "export PORT=%d TMUX_PREFIX='%s' SESSIONS_DIR='%s'",
             PORT, TMUX_PREFIX, SESSIONS_DIR);
    tmux_send(tmux_name, env_line, 1);
    tmux_send_enter(tmux_name);
}

static void scan_tmux_sessions(struct session_list *registered, struct strlist *unregistered) {
    char cmd[256];
    snprintf(cmd, sizeof(cmd), "%s list-sessions -F '#{session_name}'", TMUX_BIN);
    char *out = run_cmd_capture(cmd);
    if (!out) return;

    char *line = strtok(out, "\n");
    while (line) {
        char *session = trim_whitespace(line);
        if (*session) {
            if (starts_with(session, TMUX_PREFIX)) {
                const char *name = session + strlen(TMUX_PREFIX);
                session_list_add(registered, name, session);
            } else {
                char *pane = get_pane_command(session);
                char *lower = str_tolower_copy(pane);
                if (strstr(lower, "claude") != NULL || strcmp(session, "claude") == 0) {
                    strlist_add(unregistered, session);
                }
                free(pane);
                free(lower);
            }
        }
        line = strtok(NULL, "\n");
    }
    free(out);
    session_list_sort(registered);
}

static void get_registered_sessions(struct session_list *registered) {
    if (registered->len == 0) {
        struct strlist unregistered = {0};
        scan_tmux_sessions(registered, &unregistered);
        strlist_free(&unregistered);
    }

    if (state.active) {
        if (!session_list_find(registered, state.active)) {
            free(state.active);
            state.active = NULL;
        }
    }
    if (registered->len > 0 && !state.active) {
        state.active = xstrdup(registered->items[0].name);
    }
}

static int create_session(const char *name, char *err, size_t err_len) {
    char tmux_name[256];
    snprintf(tmux_name, sizeof(tmux_name), "%s%s", TMUX_PREFIX, name);

    if (tmux_exists(tmux_name)) {
        snprintf(err, err_len, "Worker '%s' already exists", name);
        return 0;
    }

    fprintf(stderr, "Creating tmux session %s using %s\n", tmux_name, TMUX_BIN);
    char *argv_new[] = {(char *)TMUX_BIN, "new-session", "-d", "-s", tmux_name, "-x", "200", "-y", "50", NULL};
    int rc = run_tmux_args(argv_new);
    if (rc != 0) {
        fprintf(stderr, "tmux new-session failed (rc=%d, bin=%s)\n", rc, TMUX_BIN);
        snprintf(err, err_len, "Could not start the worker workspace");
        return 0;
    }

    usleep(500000);
    export_hook_env(tmux_name);
    usleep(300000);

    tmux_send(tmux_name, "claude --dangerously-skip-permissions", 1);
    tmux_send_enter(tmux_name);

    usleep(1500000);
    tmux_send(tmux_name, "2", 0);
    usleep(300000);
    tmux_send_enter(tmux_name);

    usleep(2000000);
    const char *welcome =
        "You are connected to Telegram via claudecode-telegram bridge. "
        "To send images back to Telegram, include this tag in your response: "
        "[[image:/path/to/file.png|optional caption]]. "
        "Allowed paths: /tmp, current directory. Allowed formats: jpg, png, gif, webp, bmp.";
    tmux_send_message(tmux_name, welcome);

    free(state.active);
    state.active = xstrdup(name);
    ensure_session_dir(name);

    return 1;
}

static int kill_session(const char *name, char *err, size_t err_len) {
    struct session_list registered = {0};
    get_registered_sessions(&registered);
    struct session_entry *entry = session_list_find(&registered, name);
    if (!entry) {
        snprintf(err, err_len, "Worker '%s' not found", name);
        session_list_free(&registered);
        return 0;
    }

    char *argv_kill[] = {(char *)TMUX_BIN, "kill-session", "-t", entry->tmux, NULL};
    run_tmux_args(argv_kill);
    cleanup_inbox(name);

    if (state.active && strcmp(state.active, name) == 0) {
        free(state.active);
        state.active = NULL;
    }

    session_list_free(&registered);
    return 1;
}

static int restart_claude(const char *name, char *err, size_t err_len) {
    struct session_list registered = {0};
    get_registered_sessions(&registered);
    struct session_entry *entry = session_list_find(&registered, name);
    if (!entry) {
        snprintf(err, err_len, "Worker '%s' not found", name);
        session_list_free(&registered);
        return 0;
    }
    if (!tmux_exists(entry->tmux)) {
        snprintf(err, err_len, "Worker workspace is not running");
        session_list_free(&registered);
        return 0;
    }
    if (is_claude_running(entry->tmux)) {
        snprintf(err, err_len, "Worker is already running");
        session_list_free(&registered);
        return 0;
    }
    export_hook_env(entry->tmux);
    usleep(300000);

    tmux_send(entry->tmux, "claude --dangerously-skip-permissions", 1);
    tmux_send_enter(entry->tmux);

    session_list_free(&registered);
    return 1;
}

static int switch_session(const char *name, char *err, size_t err_len) {
    struct session_list registered = {0};
    get_registered_sessions(&registered);
    if (!session_list_find(&registered, name)) {
        snprintf(err, err_len, "Worker '%s' not found", name);
        session_list_free(&registered);
        return 0;
    }
    free(state.active);
    state.active = xstrdup(name);
    session_list_free(&registered);
    return 1;
}

static int register_session(const char *name, const char *tmux_session, char *err, size_t err_len) {
    char new_tmux[256];
    snprintf(new_tmux, sizeof(new_tmux), "%s%s", TMUX_PREFIX, name);
    char *argv_rename[] = {(char *)TMUX_BIN, "rename-session", "-t", (char *)tmux_session, new_tmux, NULL};
    if (run_tmux_args(argv_rename) != 0) {
        snprintf(err, err_len, "Could not claim the running worker");
        return 0;
    }
    export_hook_env(new_tmux);
    free(state.active);
    state.active = xstrdup(name);
    free(state.pending_registration);
    state.pending_registration = NULL;
    ensure_session_dir(name);
    return 1;
}

static void *typing_thread(void *arg) {
    char *session = ((char **)arg)[0];
    long long chat_id = atoll(((char **)arg)[1]);
    while (is_pending(session)) {
        telegram_send_chat_action(chat_id);
        sleep(4);
    }
    free(((char **)arg)[0]);
    free(((char **)arg)[1]);
    free(arg);
    return NULL;
}

static void start_typing_loop(long long chat_id, const char *session_name) {
    pthread_t tid;
    char **args = xmalloc(sizeof(char *) * 2);
    args[0] = xstrdup(session_name);
    char buf[32];
    snprintf(buf, sizeof(buf), "%lld", chat_id);
    args[1] = xstrdup(buf);
    if (pthread_create(&tid, NULL, typing_thread, args) == 0) {
        pthread_detach(tid);
    }
}

static struct strlist get_all_chat_ids(void) {
    struct strlist list = {0};
    DIR *dir = opendir(SESSIONS_DIR);
    if (dir) {
        struct dirent *ent;
        while ((ent = readdir(dir)) != NULL) {
            if (strcmp(ent->d_name, ".") == 0 || strcmp(ent->d_name, "..") == 0) continue;
            char *session_dir = path_join(SESSIONS_DIR, ent->d_name);
            char *chat_file = path_join(session_dir, "chat_id");
            if (file_exists(chat_file)) {
                char *txt = read_text_file(chat_file);
                if (txt) {
                    char *trim = trim_whitespace(txt);
                    if (*trim) strlist_add_unique(&list, trim);
                    free(txt);
                }
            }
            free(chat_file);
            free(session_dir);
        }
        closedir(dir);
    }
    if (admin_known) {
        char buf[32];
        snprintf(buf, sizeof(buf), "%lld", admin_chat_id);
        strlist_add_unique(&list, buf);
    }
    return list;
}

static void send_shutdown_message(void) {
    struct strlist ids = get_all_chat_ids();
    for (size_t i = 0; i < ids.len; i++) {
        long long chat_id = atoll(ids.items[i]);
        telegram_send_message(chat_id, "Going offline briefly. Your team stays the same.", NULL);
    }
    strlist_free(&ids);
}

static void update_bot_commands(void) {
    struct session_list registered = {0};
    get_registered_sessions(&registered);
    telegram_set_commands(&registered);
    session_list_free(&registered);
}

static void send_startup_message(long long chat_id) {
    struct session_list registered = {0};
    get_registered_sessions(&registered);

    char buf[1024] = {0};
    snprintf(buf, sizeof(buf), "I'm online and ready.\n");
    if (registered.len > 0) {
        strcat(buf, "Team: ");
        for (size_t i = 0; i < registered.len; i++) {
            strcat(buf, registered.items[i].name);
            if (i + 1 < registered.len) strcat(buf, ", ");
        }
        if (state.active) {
            strcat(buf, "\nFocused: ");
            strcat(buf, state.active);
        }
    } else {
        strcat(buf, "No workers yet. Hire your first long-lived worker with /hire <name>.");
    }
    telegram_send_message(chat_id, buf, NULL);
    session_list_free(&registered);
}

static int parse_worker_prefix(const char *text, char **worker_out, char **msg_out) {
    if (!text || !*text) return 0;
    const char *colon = strstr(text, ":");
    if (!colon) return 0;
    size_t name_len = (size_t)(colon - text);
    if (name_len == 0 || name_len > 64) return 0;
    char name[128];
    snprintf(name, sizeof(name), "%.*s", (int)name_len, text);
    char *name_lc = sanitize_name(name);
    if (!*name_lc) { free(name_lc); return 0; }

    struct session_list registered = {0};
    get_registered_sessions(&registered);
    int found = session_list_find(&registered, name_lc) != NULL;
    session_list_free(&registered);
    if (!found) { free(name_lc); return 0; }

    const char *msg = colon + 1;
    while (*msg == ' ' || *msg == '\t') msg++;
    *worker_out = name_lc;
    *msg_out = xstrdup(msg);
    return 1;
}

static char *format_reply_context(const char *reply_text, const char *context_text) {
    const char *r = reply_text ? reply_text : "";
    const char *c = context_text ? context_text : "";
    size_t n = strlen(r) + strlen(c) + 64;
    char *out = xmalloc(n);
    if (c && *c) {
        snprintf(out, n, "Manager reply:\n%s\n\nContext (your previous message):\n%s", r, c);
    } else {
        snprintf(out, n, "Manager reply:\n%s", r);
    }
    return out;
}

static void route_message(const char *session_name, const char *text, long long chat_id, long long msg_id, int one_off) {
    struct session_list registered = {0};
    get_registered_sessions(&registered);
    struct session_entry *entry = session_list_find(&registered, session_name);
    if (!entry) {
        char buf[256];
        snprintf(buf, sizeof(buf), "Can't find %s. Check /team for who's available.", session_name);
        telegram_send_message(chat_id, buf, NULL);
        session_list_free(&registered);
        return;
    }

    if (!tmux_exists(entry->tmux)) {
        char buf[256];
        snprintf(buf, sizeof(buf), "%s is offline. Try /relaunch.", session_name);
        telegram_send_message(chat_id, buf, NULL);
        session_list_free(&registered);
        return;
    }

    set_pending(session_name, chat_id);
    start_typing_loop(chat_id, session_name);
    int send_ok = tmux_send_message(entry->tmux, text);
    if (msg_id && send_ok) {
        telegram_set_reaction(chat_id, msg_id);
    }
    session_list_free(&registered);
    (void)one_off;
}

static void route_to_active(const char *text, long long chat_id, long long msg_id) {
    struct session_list registered = {0};
    struct strlist unregistered = {0};
    scan_tmux_sessions(&registered, &unregistered);

    if (state.active && !session_list_find(&registered, state.active)) {
        free(state.active);
        state.active = NULL;
    }
    if (!state.active && registered.len > 0) {
        state.active = xstrdup(registered.items[0].name);
    }

    if (!state.active) {
        if (unregistered.len > 0) {
            free(state.pending_registration);
            state.pending_registration = xstrdup(unregistered.items[0]);
            telegram_send_message(chat_id,
                                  "Found a running Claude not yet on your team.\n"
                                  "Claim it to make it a long-lived worker by replying with:\n"
                                  "{\"name\": \"your-worker-name\"}",
                                  NULL);
        } else if (registered.len > 0) {
            char buf[512] = {0};
            strcat(buf, "No one assigned. Your team: ");
            for (size_t i = 0; i < registered.len; i++) {
                strcat(buf, registered.items[i].name);
                if (i + 1 < registered.len) strcat(buf, ", ");
            }
            strcat(buf, "\nWho should I talk to?");
            telegram_send_message(chat_id, buf, NULL);
        } else {
            telegram_send_message(chat_id, "No team members yet. Add someone with /hire <name>.", NULL);
        }
        session_list_free(&registered);
        strlist_free(&unregistered);
        return;
    }

    session_list_free(&registered);
    strlist_free(&unregistered);
    route_message(state.active, text, chat_id, msg_id, 0);
}

static void route_to_all(const char *text, long long chat_id, long long msg_id) {
    struct session_list registered = {0};
    get_registered_sessions(&registered);
    if (registered.len == 0) {
        telegram_send_message(chat_id, "No team members yet. Add someone with /hire <name>.", NULL);
        session_list_free(&registered);
        return;
    }
    int sent = 0;
    for (size_t i = 0; i < registered.len; i++) {
        struct session_entry *entry = &registered.items[i];
        if (tmux_exists(entry->tmux) && is_claude_running(entry->tmux)) {
            route_message(entry->name, text, chat_id, msg_id, 1);
            sent++;
        }
    }
    if (sent == 0) {
        telegram_send_message(chat_id, "No one's online to share with.", NULL);
    }
    session_list_free(&registered);
}

static int try_registration(const char *text, long long chat_id) {
    cJSON *data = cJSON_Parse(text);
    if (!data) return 0;
    cJSON *name_obj = cJSON_GetObjectItem(data, "name");
    if (!cJSON_IsString(name_obj)) {
        cJSON_Delete(data);
        return 0;
    }
    char *san = sanitize_name(name_obj->valuestring);
    if (!*san) {
        telegram_send_message(chat_id, "Name must use letters, numbers, and hyphens only.", NULL);
        free(san);
        cJSON_Delete(data);
        return 1;
    }
    if (is_reserved_name(san)) {
        char buf[256];
        snprintf(buf, sizeof(buf), "Cannot use \"%s\" - reserved command. Choose another name.", san);
        telegram_send_message(chat_id, buf, NULL);
        free(san);
        cJSON_Delete(data);
        return 1;
    }
    struct session_list registered = {0};
    get_registered_sessions(&registered);
    if (session_list_find(&registered, san)) {
        char buf[256];
        snprintf(buf, sizeof(buf), "Worker name \"%s\" is already on the team. Choose another.", san);
        telegram_send_message(chat_id, buf, NULL);
        free(san);
        session_list_free(&registered);
        cJSON_Delete(data);
        return 1;
    }
    char err[128] = {0};
    if (register_session(san, state.pending_registration, err, sizeof(err))) {
        char buf[256];
        snprintf(buf, sizeof(buf), "%s is now on your team and assigned.", san);
        telegram_send_message(chat_id, buf, NULL);
        update_bot_commands();
    } else {
        char buf[256];
        snprintf(buf, sizeof(buf), "Could not claim that worker. %s", err);
        telegram_send_message(chat_id, buf, NULL);
    }
    free(san);
    session_list_free(&registered);
    cJSON_Delete(data);
    return 1;
}

static int cmd_hire(const char *arg, long long chat_id) {
    if (!arg || !*arg) {
        telegram_send_message(chat_id, "Usage: /hire <name>", NULL);
        return 1;
    }
    char *san = sanitize_name(arg);
    fprintf(stderr, "cmd_hire: arg='%s' sanitized='%s'\n", arg, san);
    if (!*san) {
        telegram_send_message(chat_id, "Name must use letters, numbers, and hyphens only.", NULL);
        free(san);
        return 1;
    }
    if (is_reserved_name(san)) {
        char buf[256];
        snprintf(buf, sizeof(buf), "Cannot use \"%s\" - reserved command. Choose another name.", san);
        telegram_send_message(chat_id, buf, NULL);
        free(san);
        return 1;
    }
    char err[128] = {0};
    if (create_session(san, err, sizeof(err))) {
        char buf[256];
        snprintf(buf, sizeof(buf), "%s is added and assigned. %s", san, PERSISTENCE_NOTE);
        telegram_send_message(chat_id, buf, NULL);
        update_bot_commands();
    } else {
        fprintf(stderr, "cmd_hire: create_session failed for '%s': %s\n", san, err);
        char buf[256];
        snprintf(buf, sizeof(buf), "Could not hire \"%s\". %s", san, err);
        telegram_send_message(chat_id, buf, NULL);
    }
    free(san);
    return 1;
}

static int cmd_focus(const char *arg, long long chat_id) {
    if (!arg || !*arg) {
        telegram_send_message(chat_id, "Usage: /focus <name>", NULL);
        return 1;
    }
    char *san = sanitize_name(arg);
    char err[128] = {0};
    if (switch_session(san, err, sizeof(err))) {
        char buf[128];
        snprintf(buf, sizeof(buf), "Now talking to %s.", san);
        telegram_send_message(chat_id, buf, NULL);
    } else {
        char buf[256];
        snprintf(buf, sizeof(buf), "Could not focus \"%s\". %s", san, err);
        telegram_send_message(chat_id, buf, NULL);
    }
    free(san);
    return 1;
}

static int cmd_team(long long chat_id) {
    struct session_list registered = {0};
    struct strlist unregistered = {0};
    scan_tmux_sessions(&registered, &unregistered);
    get_registered_sessions(&registered);

    if (registered.len == 0 && unregistered.len == 0) {
        telegram_send_message(chat_id, "No team members yet. Add someone with /hire <name>.", NULL);
        session_list_free(&registered);
        strlist_free(&unregistered);
        return 1;
    }

    char buf[2048] = {0};
    strcat(buf, "Your team:\n");
    strcat(buf, "Focused: ");
    strcat(buf, state.active ? state.active : "(none)");
    strcat(buf, "\nWorkers:\n");
    for (size_t i = 0; i < registered.len; i++) {
        const char *name = registered.items[i].name;
        strcat(buf, "- ");
        strcat(buf, name);
        strcat(buf, " (");
        if (state.active && strcmp(state.active, name) == 0) {
            strcat(buf, "focused, ");
        }
        strcat(buf, is_pending(name) ? "working" : "available");
        strcat(buf, ")\n");
    }

    if (unregistered.len > 0) {
        strcat(buf, "\nUnclaimed running Claude (needs a name):\n");
        for (size_t i = 0; i < unregistered.len; i++) {
            strcat(buf, "- ");
            strcat(buf, unregistered.items[i]);
            strcat(buf, "\n");
        }
    }

    telegram_send_message(chat_id, buf, NULL);
    session_list_free(&registered);
    strlist_free(&unregistered);
    return 1;
}

static int cmd_end(const char *arg, long long chat_id) {
    if (!arg || !*arg) {
        telegram_send_message(chat_id, "Offboarding is permanent. Usage: /end <name>", NULL);
        return 1;
    }
    char *san = sanitize_name(arg);
    char err[128] = {0};
    if (kill_session(san, err, sizeof(err))) {
        char buf[128];
        snprintf(buf, sizeof(buf), "%s removed from your team.", san);
        telegram_send_message(chat_id, buf, NULL);
        update_bot_commands();
    } else {
        char buf[256];
        snprintf(buf, sizeof(buf), "Could not offboard \"%s\". %s", san, err);
        telegram_send_message(chat_id, buf, NULL);
    }
    free(san);
    return 1;
}

static int cmd_progress(long long chat_id) {
    if (!state.active) {
        telegram_send_message(chat_id, "No one assigned. Who should I talk to? Use /team or /focus <name>.", NULL);
        return 1;
    }
    const char *name = state.active;
    struct session_list registered = {0};
    get_registered_sessions(&registered);
    struct session_entry *entry = session_list_find(&registered, name);
    if (!entry) {
        telegram_send_message(chat_id, "Can't find them. Check /team for who's available.", NULL);
        session_list_free(&registered);
        return 1;
    }
    int exists = tmux_exists(entry->tmux);
    int pending = is_pending(name);
    char buf[512] = {0};
    snprintf(buf, sizeof(buf), "Progress for focused worker: %s\nFocused: yes\nWorking: %s\nOnline: %s",
             name, pending ? "yes" : "no", exists ? "yes" : "no");
    if (exists) {
        int ready = is_claude_running(entry->tmux);
        strcat(buf, "\nReady: ");
        strcat(buf, ready ? "yes" : "no");
        if (!ready) strcat(buf, "\nNeeds attention: worker app is not running. Use /relaunch.");
    }
    telegram_send_message(chat_id, buf, NULL);
    session_list_free(&registered);
    return 1;
}

static int cmd_pause(long long chat_id) {
    if (!state.active) {
        telegram_send_message(chat_id, "No one assigned.", NULL);
        return 1;
    }
    struct session_list registered = {0};
    get_registered_sessions(&registered);
    struct session_entry *entry = session_list_find(&registered, state.active);
    if (entry) {
        tmux_send_escape(entry->tmux);
        clear_pending(state.active);
    }
    char buf[128];
    snprintf(buf, sizeof(buf), "%s is paused. I'll pick up where we left off.", state.active);
    telegram_send_message(chat_id, buf, NULL);
    session_list_free(&registered);
    return 1;
}

static int cmd_relaunch(long long chat_id) {
    if (!state.active) {
        telegram_send_message(chat_id, "No one assigned.", NULL);
        return 1;
    }
    char err[128] = {0};
    if (restart_claude(state.active, err, sizeof(err))) {
        char buf[128];
        snprintf(buf, sizeof(buf), "Bringing %s back online...", state.active);
        telegram_send_message(chat_id, buf, NULL);
    } else {
        char buf[256];
        snprintf(buf, sizeof(buf), "Could not relaunch \"%s\". %s", state.active, err);
        telegram_send_message(chat_id, buf, NULL);
    }
    return 1;
}

static int cmd_settings(long long chat_id) {
    char token_red[32] = "(not set)";
    if (BOT_TOKEN && *BOT_TOKEN) {
        size_t len = strlen(BOT_TOKEN);
        if (len <= 8) {
            snprintf(token_red, sizeof(token_red), "***");
        } else {
            snprintf(token_red, sizeof(token_red), "%c%c%c%c...%c%c%c%c",
                     BOT_TOKEN[0], BOT_TOKEN[1], BOT_TOKEN[2], BOT_TOKEN[3],
                     BOT_TOKEN[len - 4], BOT_TOKEN[len - 3], BOT_TOKEN[len - 2], BOT_TOKEN[len - 1]);
        }
    }
    char webhook_red[32] = "(disabled)";
    if (WEBHOOK_SECRET && *WEBHOOK_SECRET) {
        size_t len = strlen(WEBHOOK_SECRET);
        if (len <= 8) {
            snprintf(webhook_red, sizeof(webhook_red), "***");
        } else {
            snprintf(webhook_red, sizeof(webhook_red), "%c%c%c%c...%c%c%c%c",
                     WEBHOOK_SECRET[0], WEBHOOK_SECRET[1], WEBHOOK_SECRET[2], WEBHOOK_SECRET[3],
                     WEBHOOK_SECRET[len - 4], WEBHOOK_SECRET[len - 3], WEBHOOK_SECRET[len - 2], WEBHOOK_SECRET[len - 1]);
        }
    }

    struct session_list registered = {0};
    get_registered_sessions(&registered);
    char team[512] = "";
    if (registered.len == 0) {
        snprintf(team, sizeof(team), "(none)");
    } else {
        for (size_t i = 0; i < registered.len; i++) {
            strcat(team, registered.items[i].name);
            if (i + 1 < registered.len) strcat(team, ", ");
        }
    }

    char admin_buf[64] = "(auto-learn)";
    if (admin_known) snprintf(admin_buf, sizeof(admin_buf), "%lld", admin_chat_id);

    char team_storage[PATH_MAX];
    snprintf(team_storage, sizeof(team_storage), "%s", SESSIONS_DIR);
    char *slash = strrchr(team_storage, '/');
    if (slash) *slash = '\0';

    char buf[1024];
    snprintf(buf, sizeof(buf),
             "claudecode-telegram v%s\n%s\n\nBot token: %s\nAdmin: %s\nWebhook verification: %s\nTeam storage: %s\n\n"
             "Team state\nFocused worker: %s\nWorkers: %s\nPending claim: %s",
             VERSION, PERSISTENCE_NOTE, token_red,
             admin_buf,
             webhook_red, team_storage,
             state.active ? state.active : "(none)",
             team,
             state.pending_registration ? state.pending_registration : "(none)");

    telegram_send_message(chat_id, buf, NULL);
    session_list_free(&registered);
    return 1;
}

static int cmd_learn(const char *topic, long long chat_id, long long msg_id) {
    if (!state.active) {
        telegram_send_message(chat_id, "No one assigned. Who should I talk to?", NULL);
        return 1;
    }
    struct session_list registered = {0};
    get_registered_sessions(&registered);
    struct session_entry *entry = session_list_find(&registered, state.active);
    if (!entry) {
        telegram_send_message(chat_id, "Can't find them. Check /team.", NULL);
        session_list_free(&registered);
        return 1;
    }
    if (!tmux_exists(entry->tmux) || !is_claude_running(entry->tmux)) {
        char buf[128];
        snprintf(buf, sizeof(buf), "%s is offline. Try /relaunch.", state.active);
        telegram_send_message(chat_id, buf, NULL);
        session_list_free(&registered);
        return 1;
    }

    char prompt[512];
    if (topic && *topic) {
        snprintf(prompt, sizeof(prompt),
                 "What did you learn about %s today? Please answer in Problem / Fix / Why format:\n"
                 "Problem: <what went wrong or was inefficient>\n"
                 "Fix: <the better approach>\n"
                 "Why: <root cause or insight>",
                 topic);
    } else {
        snprintf(prompt, sizeof(prompt),
                 "What did you learn today? Please answer in Problem / Fix / Why format:\n"
                 "Problem: <what went wrong or was inefficient>\n"
                 "Fix: <the better approach>\n"
                 "Why: <root cause or insight>");
    }

    set_pending(state.active, chat_id);
    start_typing_loop(chat_id, state.active);
    int send_ok = tmux_send_message(entry->tmux, prompt);
    if (msg_id && send_ok) telegram_set_reaction(chat_id, msg_id);

    session_list_free(&registered);
    return 1;
}

static int handle_command(const char *text, long long chat_id, long long msg_id) {
    char *space = strchr(text, ' ');
    char cmd_buf[128];
    char *arg = NULL;
    if (space) {
        size_t len = (size_t)(space - text);
        snprintf(cmd_buf, sizeof(cmd_buf), "%.*s", (int)len, text);
        arg = (char *)(space + 1);
    } else {
        snprintf(cmd_buf, sizeof(cmd_buf), "%s", text);
    }
    char *cmd = str_tolower_copy(cmd_buf);
    char *at = strchr(cmd, '@');
    if (at) *at = '\0';

    char *arg_trim = trim_copy(arg ? arg : "");
    fprintf(stderr, "handle_command: cmd='%s' arg='%s'\n", cmd, arg_trim);

    if (strcmp(cmd, "/hire") == 0 || strcmp(cmd, "/new") == 0) {
        int rc = cmd_hire(arg_trim, chat_id);
        free(arg_trim);
        free(cmd);
        return rc;
    } else if (strcmp(cmd, "/focus") == 0 || strcmp(cmd, "/use") == 0) {
        int rc = cmd_focus(arg_trim, chat_id);
        free(arg_trim);
        free(cmd);
        return rc;
    } else if (strcmp(cmd, "/team") == 0 || strcmp(cmd, "/list") == 0) {
        int rc = cmd_team(chat_id);
        free(arg_trim);
        free(cmd);
        return rc;
    } else if (strcmp(cmd, "/end") == 0 || strcmp(cmd, "/kill") == 0) {
        int rc = cmd_end(arg_trim, chat_id);
        free(arg_trim);
        free(cmd);
        return rc;
    } else if (strcmp(cmd, "/progress") == 0 || strcmp(cmd, "/status") == 0) {
        int rc = cmd_progress(chat_id);
        free(arg_trim);
        free(cmd);
        return rc;
    } else if (strcmp(cmd, "/pause") == 0 || strcmp(cmd, "/stop") == 0) {
        int rc = cmd_pause(chat_id);
        free(arg_trim);
        free(cmd);
        return rc;
    } else if (strcmp(cmd, "/relaunch") == 0 || strcmp(cmd, "/restart") == 0) {
        int rc = cmd_relaunch(chat_id);
        free(arg_trim);
        free(cmd);
        return rc;
    } else if (strcmp(cmd, "/settings") == 0 || strcmp(cmd, "/system") == 0) {
        int rc = cmd_settings(chat_id);
        free(arg_trim);
        free(cmd);
        return rc;
    } else if (strcmp(cmd, "/learn") == 0) {
        int rc = cmd_learn(arg_trim, chat_id, msg_id);
        free(arg_trim);
        free(cmd);
        return rc;
    } else if (is_blocked_command(cmd)) {
        char buf[128];
        snprintf(buf, sizeof(buf), "%s is interactive and not supported here.", cmd);
        telegram_send_message(chat_id, buf, NULL);
        free(arg_trim);
        free(cmd);
        return 1;
    }

    if (cmd[0] == '/' && strlen(cmd) > 1) {
        const char *worker = cmd + 1;
        struct session_list registered = {0};
        get_registered_sessions(&registered);
        struct session_entry *entry = session_list_find(&registered, worker);
        if (entry) {
            char *prev = state.active ? xstrdup(state.active) : NULL;
            free(state.active);
            state.active = xstrdup(worker);
            if (!arg || !*arg) {
                char buf[128];
                snprintf(buf, sizeof(buf), "Now talking to %s.", worker);
                telegram_send_message(chat_id, buf, NULL);
                free(prev);
                session_list_free(&registered);
                free(arg_trim);
                free(cmd);
                return 1;
            }
            if (!prev || strcmp(prev, worker) != 0) {
                char buf[128];
                snprintf(buf, sizeof(buf), "Now talking to %s.", worker);
                telegram_send_message(chat_id, buf, NULL);
            }
            route_message(worker, arg_trim, chat_id, msg_id, 0);
            free(prev);
            session_list_free(&registered);
            free(arg_trim);
            free(cmd);
            return 1;
        }
        session_list_free(&registered);
    }

    free(arg_trim);
    free(cmd);
    return 0;
}

static void handle_hook_response(const char *body) {
    cJSON *data = cJSON_Parse(body);
    if (!data) return;
    cJSON *sess = cJSON_GetObjectItem(data, "session");
    cJSON *text_obj = cJSON_GetObjectItem(data, "text");
    if (!cJSON_IsString(sess) || !cJSON_IsString(text_obj)) {
        cJSON_Delete(data);
        return;
    }
    const char *session_name = sess->valuestring;
    const char *text = text_obj->valuestring;

    char *chat_file = get_chat_id_file(session_name);
    if (!file_exists(chat_file)) {
        free(chat_file);
        cJSON_Delete(data);
        return;
    }
    char *chat_txt = read_text_file(chat_file);
    free(chat_file);
    if (!chat_txt) {
        cJSON_Delete(data);
        return;
    }
    long long chat_id = atoll(chat_txt);
    free(chat_txt);

    struct image_list images = {0};
    char *clean_text = parse_image_tags(text, &images);
    if (clean_text && *clean_text) {
        char *resp_text = format_response_text(session_name, clean_text);
        telegram_send_message(chat_id, resp_text, "HTML");
        free(resp_text);
    }
    free(clean_text);

    for (size_t i = 0; i < images.len; i++) {
        char caption[512];
        if (images.items[i].caption && *images.items[i].caption) {
            snprintf(caption, sizeof(caption), "%s: %s", session_name, images.items[i].caption);
        } else {
            snprintf(caption, sizeof(caption), "%s:", session_name);
        }
        if (!send_photo(chat_id, images.items[i].path, caption)) {
            char fallback[512];
            snprintf(fallback, sizeof(fallback), "<b>%s:</b> [Image failed: %s]", session_name, images.items[i].path);
            telegram_send_message(chat_id, fallback, "HTML");
        }
    }
    image_list_free(&images);
    clear_pending(session_name);
    cJSON_Delete(data);
}

static void handle_notify(const char *body) {
    cJSON *data = cJSON_Parse(body);
    if (!data) return;
    cJSON *text = cJSON_GetObjectItem(data, "text");
    if (!cJSON_IsString(text) || !text->valuestring) {
        cJSON_Delete(data);
        return;
    }
    struct strlist ids = get_all_chat_ids();
    for (size_t i = 0; i < ids.len; i++) {
        telegram_send_message(atoll(ids.items[i]), text->valuestring, NULL);
    }
    strlist_free(&ids);
    cJSON_Delete(data);
}

static void handle_message_update(const char *body) {
    cJSON *update = cJSON_Parse(body);
    if (!update) return;
    cJSON *msg = cJSON_GetObjectItem(update, "message");
    if (!cJSON_IsObject(msg)) {
        cJSON_Delete(update);
        return;
    }

    cJSON *text_obj = cJSON_GetObjectItem(msg, "text");
    cJSON *caption_obj = cJSON_GetObjectItem(msg, "caption");
    const char *text = cJSON_IsString(text_obj) ? text_obj->valuestring : NULL;
    if (!text && cJSON_IsString(caption_obj)) text = caption_obj->valuestring;

    cJSON *chat = cJSON_GetObjectItem(msg, "chat");
    long long chat_id = 0;
    if (cJSON_IsObject(chat)) {
        cJSON *id = cJSON_GetObjectItem(chat, "id");
        if (cJSON_IsNumber(id)) chat_id = (long long)id->valuedouble;
    }
    cJSON *msg_id_obj = cJSON_GetObjectItem(msg, "message_id");
    long long msg_id = cJSON_IsNumber(msg_id_obj) ? (long long)msg_id_obj->valuedouble : 0;

    cJSON *photo = cJSON_GetObjectItem(msg, "photo");
    cJSON *document = cJSON_GetObjectItem(msg, "document");
    int doc_is_image = 0;
    const char *doc_file_id = NULL;
    if (cJSON_IsObject(document)) {
        cJSON *mime = cJSON_GetObjectItem(document, "mime_type");
        if (cJSON_IsString(mime) && starts_with(mime->valuestring, "image/")) {
            doc_is_image = 1;
            cJSON *fid = cJSON_GetObjectItem(document, "file_id");
            if (cJSON_IsString(fid)) doc_file_id = fid->valuestring;
        }
    }

    if ((cJSON_IsArray(photo) || doc_is_image) && chat_id) {
        if (!admin_known) {
            admin_chat_id = chat_id;
            admin_known = 1;
        } else if (chat_id != admin_chat_id) {
            cJSON_Delete(update);
            return;
        }

        if (!state.active) {
            telegram_send_message(chat_id, "Needs decision - No focused worker. Use /focus <name> first.", NULL);
            cJSON_Delete(update);
            return;
        }

        const char *file_id = NULL;
        if (cJSON_IsArray(photo)) {
            size_t best_size = 0;
            int n = cJSON_GetArraySize(photo);
            for (int i = 0; i < n; i++) {
                cJSON *p = cJSON_GetArrayItem(photo, i);
                if (!cJSON_IsObject(p)) continue;
                cJSON *fs = cJSON_GetObjectItem(p, "file_size");
                cJSON *fid = cJSON_GetObjectItem(p, "file_id");
                size_t sz = cJSON_IsNumber(fs) ? (size_t)fs->valuedouble : 0;
                if (cJSON_IsString(fid) && sz >= best_size) {
                    best_size = sz;
                    file_id = fid->valuestring;
                }
            }
        } else if (doc_is_image) {
            file_id = doc_file_id;
        }

        if (file_id) {
            char *local_path = download_telegram_file(file_id, state.active);
            if (local_path) {
                char msgbuf[1024];
                if (text && *text) {
                    snprintf(msgbuf, sizeof(msgbuf), "%s\n\nManager sent image: %s", text, local_path);
                } else {
                    snprintf(msgbuf, sizeof(msgbuf), "Manager sent image: %s", local_path);
                }
                route_to_active(msgbuf, chat_id, msg_id);
                free(local_path);
            } else {
                telegram_send_message(chat_id, "Needs decision - Could not download image. Try again or send as file.", NULL);
            }
        }
        cJSON_Delete(update);
        return;
    }

    if (!text || !chat_id) {
        cJSON_Delete(update);
        return;
    }

    fprintf(stderr, "incoming message chat_id=%lld text='%s'\n", chat_id, text);

    if (!admin_known) {
        admin_chat_id = chat_id;
        admin_known = 1;
    }

    if (!state.startup_notified) {
        state.startup_notified = 1;
        send_startup_message(chat_id);
    }

    if (chat_id != admin_chat_id) {
        cJSON_Delete(update);
        return;
    }

    if (state.pending_registration) {
        if (try_registration(text, chat_id)) {
            cJSON_Delete(update);
            return;
        }
    }

    if (text[0] == '/') {
        if (handle_command(text, chat_id, msg_id)) {
            cJSON_Delete(update);
            return;
        }
    }

    if (strncasecmp(text, "@all ", 5) == 0) {
        route_to_all(text + 5, chat_id, msg_id);
        cJSON_Delete(update);
        return;
    }

    cJSON *reply_to = cJSON_GetObjectItem(msg, "reply_to_message");
    const char *reply_text = NULL;
    if (cJSON_IsObject(reply_to)) {
        cJSON *rt = cJSON_GetObjectItem(reply_to, "text");
        cJSON *rc = cJSON_GetObjectItem(reply_to, "caption");
        if (cJSON_IsString(rt)) reply_text = rt->valuestring;
        else if (cJSON_IsString(rc)) reply_text = rc->valuestring;
    }

    char *target = NULL;
    char *message = NULL;
    if (text[0] == '@') {
        const char *space = text + 1;
        while (*space && !isspace((unsigned char)*space)) space++;
        if (*space && space > text + 1) {
            char name[128];
            snprintf(name, sizeof(name), "%.*s", (int)(space - text - 1), text + 1);
            char *san = sanitize_name(name);
            struct session_list registered = {0};
            get_registered_sessions(&registered);
            if (session_list_find(&registered, san)) {
                target = san;
                while (*space && isspace((unsigned char)*space)) space++;
                message = xstrdup(space);
            } else {
                free(san);
            }
            session_list_free(&registered);
        }
    }

    char *reply_target = NULL;
    char *reply_context = NULL;
    if (cJSON_IsObject(reply_to) && reply_text) {
        cJSON *reply_from = cJSON_GetObjectItem(reply_to, "from");
        if (cJSON_IsObject(reply_from)) {
            cJSON *is_bot = cJSON_GetObjectItem(reply_from, "is_bot");
            if (cJSON_IsBool(is_bot) && cJSON_IsTrue(is_bot)) {
                char *worker = NULL;
                char *msg2 = NULL;
                if (parse_worker_prefix(reply_text, &worker, &msg2)) {
                    reply_target = worker;
                    free(msg2);
                }
            }
        }
        reply_context = reply_text ? xstrdup(reply_text) : NULL;
    }

    if (target) {
        if (reply_context) {
            char *formatted = format_reply_context(message, reply_context);
            route_message(target, formatted, chat_id, msg_id, 1);
            free(formatted);
        } else {
            route_message(target, message, chat_id, msg_id, 1);
        }
        free(target);
        free(message);
        free(reply_context);
        cJSON_Delete(update);
        return;
    }

    if (reply_context) {
        if (reply_target) {
            char *formatted = format_reply_context(text, reply_context);
            route_message(reply_target, formatted, chat_id, msg_id, 1);
            free(formatted);
        } else {
            char *formatted = format_reply_context(text, reply_context);
            route_to_active(formatted, chat_id, msg_id);
            free(formatted);
        }
        free(reply_target);
        free(reply_context);
        cJSON_Delete(update);
        return;
    }

    route_to_active(text, chat_id, msg_id);
    cJSON_Delete(update);
}

struct connection_info {
    char *data;
    size_t size;
};

static enum MHD_Result send_response(struct MHD_Connection *connection, unsigned int status, const char *body) {
    struct MHD_Response *response = MHD_create_response_from_buffer(strlen(body), (void *)body, MHD_RESPMEM_MUST_COPY);
    enum MHD_Result ret = MHD_queue_response(connection, status, response);
    MHD_destroy_response(response);
    return ret;
}

static enum MHD_Result handle_request(void *cls, struct MHD_Connection *connection, const char *url,
                                      const char *method, const char *version,
                                      const char *upload_data, size_t *upload_data_size, void **con_cls) {
    (void)cls; (void)version;

    if (strcmp(method, "GET") == 0) {
        return send_response(connection, MHD_HTTP_OK, "Claude-Telegram Multi-Session Bridge");
    }

    if (strcmp(method, "POST") != 0) {
        return send_response(connection, MHD_HTTP_METHOD_NOT_ALLOWED, "Method Not Allowed");
    }

    struct connection_info *info = *con_cls;
    if (!info) {
        info = xmalloc(sizeof(*info));
        info->data = NULL;
        info->size = 0;
        *con_cls = info;
        return MHD_YES;
    }

    if (*upload_data_size != 0) {
        char *newp = realloc(info->data, info->size + *upload_data_size + 1);
        if (!newp) return MHD_NO;
        info->data = newp;
        memcpy(info->data + info->size, upload_data, *upload_data_size);
        info->size += *upload_data_size;
        info->data[info->size] = '\0';
        *upload_data_size = 0;
        return MHD_YES;
    }

    const char *body = info->data ? info->data : "";

    if (strcmp(url, "/response") == 0) {
        handle_hook_response(body);
        free(info->data);
        free(info);
        *con_cls = NULL;
        return send_response(connection, MHD_HTTP_OK, "OK");
    }

    if (strcmp(url, "/notify") == 0) {
        handle_notify(body);
        free(info->data);
        free(info);
        *con_cls = NULL;
        return send_response(connection, MHD_HTTP_OK, "OK");
    }

    if (WEBHOOK_SECRET && *WEBHOOK_SECRET) {
        const char *token = MHD_lookup_connection_value(connection, MHD_HEADER_KIND,
                                                        "X-Telegram-Bot-Api-Secret-Token");
        if (!token || strcmp(token, WEBHOOK_SECRET) != 0) {
            free(info->data);
            free(info);
            *con_cls = NULL;
            return send_response(connection, MHD_HTTP_FORBIDDEN, "Forbidden");
        }
    }

    handle_message_update(body);
    free(info->data);
    free(info);
    *con_cls = NULL;
    return send_response(connection, MHD_HTTP_OK, "OK");
}

static void handle_signal(int sig) {
    (void)sig;
    shutdown_requested = 1;
}

static void init_env(void) {
    BOT_TOKEN = getenv("TELEGRAM_BOT_TOKEN");
    const char *port_env = getenv("PORT");
    if (port_env && *port_env) PORT = atoi(port_env);

    WEBHOOK_SECRET = getenv("TELEGRAM_WEBHOOK_SECRET");
    TMUX_PREFIX = getenv("TMUX_PREFIX");
    if (!TMUX_PREFIX || !*TMUX_PREFIX) TMUX_PREFIX = "claude-";

    const char *tmux_env = getenv("TMUX_BIN");
    if (tmux_env && *tmux_env) TMUX_BIN = tmux_env;

    // Avoid stale tmux socket envs breaking tmux commands.
    unsetenv("TMUX");
    unsetenv("TMUX_PANE");

    const char *sessions_env = getenv("SESSIONS_DIR");
    if (sessions_env && *sessions_env) {
        SESSIONS_DIR = xstrdup(sessions_env);
    } else {
        const char *home = getenv("HOME");
        if (!home) home = ".";
        size_t n = strlen(home) + 32;
        SESSIONS_DIR = xmalloc(n);
        snprintf(SESSIONS_DIR, n, "%s/.claude/telegram/sessions", home);
    }

    const char *admin_env = getenv("ADMIN_CHAT_ID");
    if (admin_env && *admin_env) {
        admin_chat_id = atoll(admin_env);
        admin_known = 1;
    }
}

int main(void) {
    setvbuf(stdout, NULL, _IOLBF, 0);
    setvbuf(stderr, NULL, _IONBF, 0);
    init_env();
    if (!BOT_TOKEN || !*BOT_TOKEN) {
        fprintf(stderr, "Error: TELEGRAM_BOT_TOKEN not set\n");
        return 1;
    }

    curl_global_init(CURL_GLOBAL_DEFAULT);

    mkdir_p(SESSIONS_DIR, 0700);
    chmod(SESSIONS_DIR, 0700);

    char *parent = xstrdup(SESSIONS_DIR);
    char *slash = strrchr(parent, '/');
    if (slash) {
        *slash = '\0';
        char port_path[PATH_MAX];
        snprintf(port_path, sizeof(port_path), "%s/port", parent);
        char port_buf[16];
        snprintf(port_buf, sizeof(port_buf), "%d", PORT);
        write_text_file(port_path, port_buf, 0600);
    }
    free(parent);

    struct session_list registered = {0};
    struct strlist unregistered = {0};
    scan_tmux_sessions(&registered, &unregistered);
    get_registered_sessions(&registered);

    if (registered.len > 0) {
        fprintf(stdout, "Discovered sessions: ");
        for (size_t i = 0; i < registered.len; i++) {
            fprintf(stdout, "%s%s", registered.items[i].name, i + 1 < registered.len ? ", " : "\n");
        }
    }
    if (unregistered.len > 0) {
        fprintf(stdout, "Unregistered sessions: ");
        for (size_t i = 0; i < unregistered.len; i++) {
            fprintf(stdout, "%s%s", unregistered.items[i], i + 1 < unregistered.len ? ", " : "\n");
        }
    }

    update_bot_commands();

    fprintf(stdout, "Multi-Session Bridge on :%d\n", PORT);
    fprintf(stdout, "Hook endpoint: http://localhost:%d/response\n", PORT);
    fprintf(stdout, "Active: %s\n", state.active ? state.active : "none");
    fprintf(stdout, "Sessions: %s\n", registered.len ? "(see above)" : "none");
    fprintf(stdout, "Webhook verification: %s\n", (WEBHOOK_SECRET && *WEBHOOK_SECRET) ? "enabled" : "disabled");
    fprintf(stdout, "Admin: %s\n", admin_known ? "pre-configured" : "auto-learn");
    fprintf(stdout, "tmux bin: %s\n", TMUX_BIN);

    session_list_free(&registered);
    strlist_free(&unregistered);

    signal(SIGINT, handle_signal);
    signal(SIGTERM, handle_signal);

    server_daemon = MHD_start_daemon(MHD_USE_INTERNAL_POLLING_THREAD, (unsigned short)PORT,
                                     NULL, NULL, &handle_request, NULL, MHD_OPTION_END);
    if (!server_daemon) {
        fprintf(stderr, "Failed to start HTTP server\n");
        return 1;
    }

    while (!shutdown_requested) {
        sleep(1);
    }

    send_shutdown_message();
    if (server_daemon) MHD_stop_daemon(server_daemon);
    curl_global_cleanup();
    return 0;
}
