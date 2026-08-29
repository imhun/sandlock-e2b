/*
 * Transparent egress SOCKS5 tunnel for LD_PRELOAD (phase B1).
 *
 * Loaded into the sandboxed process via LD_PRELOAD. It intercepts
 * getaddrinfo() and connect() so the sandboxed app is unaware:
 *
 *  - getaddrinfo() maps every hostname to a stable synthetic loopback IP
 *    (127.0.0.2/8) and records the hostname. No DNS leaves the sandbox.
 *  - connect() resolves the destination back to its hostname (or keeps a
 *    literal IP), applies the allowOut/denyOut filter passed via EGRESS_ALLOW
 *    / EGRESS_DENY, then dials the user's SOCKS5 proxy (EGRESS_PROXY) and
 *    performs the RFC 1928 handshake with ATYP=domain for hostnames (remote
 *    DNS, per the E2B egress proxy semantics) or ATYP=IPv4 for literal IPs.
 *
 * Fail closed: an unreachable proxy or a failed handshake surfaces as a
 * connect error; traffic never falls back to a direct connection. The proxy
 * address itself is dialed with the real connect() (RTLD_NEXT), so sandlock's
 * seccomp on-behalf path only ever sees the proxy endpoint — the sandbox's
 * net_allow must permit exactly that one endpoint.
 */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <netdb.h>
#include <netinet/in.h>
#include <poll.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <unistd.h>

/* ---------------------------------------------------------------- config */

/* 127.0.0.2 as the host-order value of its network-byte-order form. */
#define SYNTH_NET ((in_addr_t)0x7f000002u)
#define MAX_HOSTNAME 255
#define MAX_RULES 256
#define HANDSHAKE_TIMEOUT_MS 10000

struct rule {
    int kind; /* 0=domain exact, 1=domain suffix (*.x), 2=ip, 3=cidr */
    char text[MAX_HOSTNAME + 1];
    struct in_addr ip;
    struct in_addr mask;
};

static struct {
    int enabled;
    char proxy_ip[64];
    unsigned short proxy_port;
    char proxy_user[256];
    char proxy_pass[256];
    int has_auth;
    struct rule allow[MAX_RULES];
    int allow_count;
    struct rule deny[MAX_RULES];
    int deny_count;
} g_cfg;

/* hostname -> synthetic IP mapping */
struct host_map {
    char host[MAX_HOSTNAME + 1];
    in_addr_t ip;
    struct host_map *next;
};

static struct {
    struct host_map *head;
    in_addr_t next_off;
    pthread_mutex_t lock;
} g_hosts = {.next_off = 0, .lock = PTHREAD_MUTEX_INITIALIZER};

static int g_init_done;
static pthread_mutex_t g_init_lock = PTHREAD_MUTEX_INITIALIZER;
static int g_debug;

static int (*real_connect_fn)(int, const struct sockaddr *, socklen_t);
static int (*real_getaddrinfo_fn)(const char *, const char *,
                                  const struct addrinfo *, struct addrinfo **);

static void resolve_real_fns(void) {
    static pthread_mutex_t once = PTHREAD_MUTEX_INITIALIZER;
    pthread_mutex_lock(&once);
    if (real_connect_fn == NULL)
        real_connect_fn = (int (*)(int, const struct sockaddr *, socklen_t))
            dlsym(RTLD_NEXT, "connect");
    if (real_getaddrinfo_fn == NULL)
        real_getaddrinfo_fn = (int (*)(const char *, const char *,
                                       const struct addrinfo *,
                                       struct addrinfo **))
            dlsym(RTLD_NEXT, "getaddrinfo");
    pthread_mutex_unlock(&once);
}

/* ----------------------------------------------------------------- util */

static void log_msg(const char *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    vfprintf(stderr, fmt, ap);
    va_end(ap);
    fputc('\n', stderr);
}

static int parse_cidr(const char *text, struct in_addr *ip, struct in_addr *mask) {
    char buf[128];
    char *slash;
    unsigned long bits;
    if (strlen(text) >= sizeof(buf)) return -1;
    strcpy(buf, text);
    slash = strchr(buf, '/');
    if (slash == NULL) return -1;
    *slash = '\0';
    if (inet_pton(AF_INET, buf, ip) != 1) return -1;
    bits = strtoul(slash + 1, NULL, 10);
    if (bits > 32) return -1;
    mask->s_addr = bits == 0 ? 0 : htonl(0xffffffffu << (32 - bits));
    return 0;
}

/* Tiny JSON string-array parser: ["a", "b", ...] — the only shape we emit.
 * Rejects escapes to stay safe; rule text never needs them. */
static int parse_string_array(const char *json, char out[][MAX_HOSTNAME + 1],
                              int max, int *count) {
    const char *p = json;
    *count = 0;
    while (*p && *p <= ' ') p++;
    if (*p++ != '[') return -1;
    while (1) {
        while (*p && *p <= ' ') p++;
        if (*p == ']') return 0;
        if (*p++ != '"') return -1;
        if (*count >= max) return -1;
        size_t n = 0;
        while (*p && *p != '"') {
            if (*p == '\\' || n >= MAX_HOSTNAME) return -1;
            out[*count][n++] = *p++;
        }
        if (*p++ != '"') return -1;
        out[*count][n] = '\0';
        (*count)++;
        while (*p && *p <= ' ') p++;
        if (*p == ',') { p++; continue; }
        if (*p == ']') return 0;
        return -1;
    }
}

static void load_rules(struct rule *rules, int *count, const char *json) {
    *count = 0;
    if (json == NULL || *json == '\0') return;
    char items[MAX_RULES][MAX_HOSTNAME + 1];
    int n = 0;
    if (parse_string_array(json, items, MAX_RULES, &n) != 0) return;
    for (int i = 0; i < n && *count < MAX_RULES; i++) {
        const char *t = items[i];
        struct rule *r = &rules[*count];
        memset(r, 0, sizeof(*r));
        if (strncmp(t, "*.", 2) == 0) {
            r->kind = 1;
            snprintf(r->text, sizeof(r->text), "%s", t + 2);
        } else if (parse_cidr(t, &r->ip, &r->mask) == 0) {
            r->kind = 3;
        } else if (inet_pton(AF_INET, t, &r->ip) == 1) {
            r->kind = 2;
        } else {
            r->kind = 0;
            snprintf(r->text, sizeof(r->text), "%s", t);
        }
        (*count)++;
    }
}

static void load_config(void) {
    pthread_mutex_lock(&g_init_lock);
    if (g_init_done) { pthread_mutex_unlock(&g_init_lock); return; }
    memset(&g_cfg, 0, sizeof(g_cfg));
    const char *proxy = getenv("EGRESS_PROXY");
    if (proxy != NULL && *proxy) {
        char host[256];
        unsigned long port = 0;
        if (sscanf(proxy, "%63[^:]:%lu", host, &port) == 2 &&
            port > 0 && port < 65536 && inet_pton(AF_INET, host, &(struct in_addr){0}) == 1) {
            snprintf(g_cfg.proxy_ip, sizeof(g_cfg.proxy_ip), "%s", host);
            g_cfg.proxy_port = (unsigned short)port;
            g_cfg.enabled = 1;
        }
    }
    const char *user = getenv("EGRESS_PROXY_USER");
    const char *pass = getenv("EGRESS_PROXY_PASS");
    if (g_cfg.enabled && user != NULL && pass != NULL) {
        snprintf(g_cfg.proxy_user, sizeof(g_cfg.proxy_user), "%s", user);
        snprintf(g_cfg.proxy_pass, sizeof(g_cfg.proxy_pass), "%s", pass);
        g_cfg.has_auth = 1;
    }
    load_rules(g_cfg.allow, &g_cfg.allow_count, getenv("EGRESS_ALLOW"));
    load_rules(g_cfg.deny, &g_cfg.deny_count, getenv("EGRESS_DENY"));
    g_debug = getenv("EGRESS_DEBUG") != NULL;
    g_init_done = 1;
    pthread_mutex_unlock(&g_init_lock);
    log_msg("egress-proxy: loaded (proxy=%s:%u auth=%d allow=%d deny=%d)",
            g_cfg.proxy_ip, g_cfg.proxy_port, g_cfg.has_auth,
            g_cfg.allow_count, g_cfg.deny_count);
}

/* --------------------------------------------------------- host mapping */

static int in_synth_range(in_addr_t ip) {
    in_addr_t v = ntohl(ip);
    return v >= SYNTH_NET && v <= 0x7fffffff;
}

static in_addr_t synth_for_host(const char *host) {
    pthread_mutex_lock(&g_hosts.lock);
    for (struct host_map *m = g_hosts.head; m != NULL; m = m->next) {
        if (strcmp(m->host, host) == 0) {
            in_addr_t ip = m->ip;
            pthread_mutex_unlock(&g_hosts.lock);
            return ip;
        }
    }
    in_addr_t ip = htonl(SYNTH_NET + g_hosts.next_off);
    g_hosts.next_off++;
    if (g_hosts.next_off > 0x7fffffff - SYNTH_NET) g_hosts.next_off = 0;
    struct host_map *m = malloc(sizeof(*m));
    if (m != NULL) {
        snprintf(m->host, sizeof(m->host), "%s", host);
        m->ip = ip;
        m->next = g_hosts.head;
        g_hosts.head = m;
    }
    pthread_mutex_unlock(&g_hosts.lock);
    return ip;
}

static int host_for_ip(in_addr_t ip, char *out, size_t out_len) {
    pthread_mutex_lock(&g_hosts.lock);
    for (struct host_map *m = g_hosts.head; m != NULL; m = m->next) {
        if (m->ip == ip) {
            snprintf(out, out_len, "%s", m->host);
            pthread_mutex_unlock(&g_hosts.lock);
            return 0;
        }
    }
    pthread_mutex_unlock(&g_hosts.lock);
    return -1;
}

/* ------------------------------------------------------- rule matching */

static int rule_matches(const struct rule *r, const char *host, in_addr_t ip) {
    switch (r->kind) {
    case 0:
        return host != NULL && strcmp(r->text, host) == 0;
    case 1: {
        if (host == NULL) return 0;
        size_t hl = strlen(host), sl = strlen(r->text);
        return hl > sl && strcmp(host + hl - sl, r->text) == 0 &&
               host[hl - sl - 1] == '.';
    }
    case 2:
        return ip != 0 && ip == r->ip.s_addr;
    case 3:
        return ip != 0 && (ip & r->mask.s_addr) == (r->ip.s_addr & r->mask.s_addr);
    }
    return 0;
}

/* E2B semantics: allowed entries take precedence; when allowOut is set the
 * default is deny; otherwise default is allow. */
static int filter_allows(const char *host, in_addr_t ip) {
    int allow_hit = 0, deny_hit = 0;
    for (int i = 0; i < g_cfg.allow_count; i++)
        if (rule_matches(&g_cfg.allow[i], host, ip)) { allow_hit = 1; break; }
    for (int i = 0; i < g_cfg.deny_count; i++)
        if (rule_matches(&g_cfg.deny[i], host, ip)) { deny_hit = 1; break; }
    if (allow_hit) return 1;
    if (deny_hit) return 0;
    return g_cfg.allow_count == 0;
}

/* ------------------------------------------------------------- SOCKS5 */

static int xsend(int fd, const void *buf, size_t len) {
    const char *p = buf;
    while (len > 0) {
        ssize_t n = send(fd, p, len, MSG_NOSIGNAL);
        if (n < 0) {
            if (errno == EINTR) continue;
            if (errno == EAGAIN || errno == EWOULDBLOCK) {
                struct pollfd pfd = {fd, POLLOUT, 0};
                if (poll(&pfd, 1, HANDSHAKE_TIMEOUT_MS) <= 0) return -1;
                continue;
            }
            return -1;
        }
        p += n;
        len -= (size_t)n;
    }
    return 0;
}

static int xrecv_exact(int fd, void *buf, size_t len) {
    char *p = buf;
    while (len > 0) {
        ssize_t n = recv(fd, p, len, 0);
        if (n < 0) {
            if (errno == EINTR) continue;
            if (errno == EAGAIN || errno == EWOULDBLOCK) {
                struct pollfd pfd = {fd, POLLIN, 0};
                if (poll(&pfd, 1, HANDSHAKE_TIMEOUT_MS) <= 0) return -1;
                continue;
            }
            return -1;
        }
        if (n == 0) return -1;
        p += n;
        len -= (size_t)n;
    }
    return 0;
}

/* Dial the proxy and run the SOCKS5 handshake for `host`/`ip`:`port`.
 * Returns 0 on success (fd is connected and tunnel ready), -1 otherwise. */
static int socks5_connect(int fd, const char *host, in_addr_t ip,
                          unsigned short port) {
    struct sockaddr_in sa;
    memset(&sa, 0, sizeof(sa));
    sa.sin_family = AF_INET;
    sa.sin_port = htons(g_cfg.proxy_port);
    if (inet_pton(AF_INET, g_cfg.proxy_ip, &sa.sin_addr) != 1) return -1;
    if (g_debug)
        log_msg("egress-proxy: dial proxy %s:%u (target %s:%u)", g_cfg.proxy_ip,
                g_cfg.proxy_port, host != NULL ? host : "(ip)", port);
    if (real_connect_fn(fd, (struct sockaddr *)&sa, sizeof(sa)) != 0) {
        if (errno == EINPROGRESS) {
            /* Non-blocking fd: wait for the proxy connect to finish, then
             * complete the handshake synchronously so the caller sees a
             * ready tunnel regardless of its blocking expectations. */
            struct pollfd pfd = {fd, POLLOUT, 0};
            int pr = poll(&pfd, 1, HANDSHAKE_TIMEOUT_MS);
            if (pr <= 0 || (pfd.revents & (POLLERR | POLLHUP | POLLNVAL))) {
                int soerr = ECONNREFUSED;
                socklen_t slen = sizeof(soerr);
                getsockopt(fd, SOL_SOCKET, SO_ERROR, &soerr, &slen);
                if (g_debug)
                    log_msg("egress-proxy: proxy connect wait failed errno=%d",
                            soerr);
                errno = soerr;
                return -1;
            }
        } else {
            if (g_debug)
                log_msg("egress-proxy: dial proxy failed errno=%d (%s)",
                        errno, strerror(errno));
            return -1;
        }
    }
    if (g_debug) log_msg("egress-proxy: proxy connected, greeting");

    unsigned char greet[4] = {0x05, 0x01, g_cfg.has_auth ? 0x02 : 0x00};
    if (xsend(fd, greet, 3) != 0) return -1;
    if (g_debug) log_msg("egress-proxy: greeting sent");
    unsigned char greply[2] = {0, 0};
    if (xrecv_exact(fd, greply, 2) != 0) return -1;
    if (g_debug)
        log_msg("egress-proxy: greeting reply %02x %02x", greply[0], greply[1]);
    if (greply[0] != 0x05 || greply[1] == 0xff) return -1;
    unsigned char method = greply[1];
    if (g_cfg.has_auth) {
        if (method != 0x02) return -1;
        size_t ul = strlen(g_cfg.proxy_user), pl = strlen(g_cfg.proxy_pass);
        if (ul > 255 || pl > 255) return -1;
        unsigned char auth[3 + 255 + 255];
        auth[0] = 0x01;
        auth[1] = (unsigned char)ul;
        memcpy(auth + 2, g_cfg.proxy_user, ul);
        auth[2 + ul] = (unsigned char)pl;
        memcpy(auth + 3 + ul, g_cfg.proxy_pass, pl);
        if (xsend(fd, auth, 3 + ul + pl) != 0) return -1;
        unsigned char areply[2] = {0, 0};
        if (xrecv_exact(fd, areply, 2) != 0) return -1;
        if (areply[0] != 0x01 || areply[1] != 0x00) return -1;
    } else if (method != 0x00) {
        return -1;
    }

    unsigned char req[4 + 1 + MAX_HOSTNAME + 2];
    size_t rl = 0;
    req[rl++] = 0x05;
    req[rl++] = 0x01;
    req[rl++] = 0x00;
    if (host != NULL) {
        size_t hl = strlen(host);
        if (hl > MAX_HOSTNAME) return -1;
        req[rl++] = 0x03;
        req[rl++] = (unsigned char)hl;
        memcpy(req + rl, host, hl);
        rl += hl;
    } else {
        req[rl++] = 0x01;
        memcpy(req + rl, &ip, 4);
        rl += 4;
    }
    req[rl++] = (unsigned char)(port >> 8);
    req[rl++] = (unsigned char)(port & 0xff);
    if (xsend(fd, req, rl) != 0) return -1;

    unsigned char head[4];
    if (xrecv_exact(fd, head, 4) != 0) return -1;
    if (head[0] != 0x05 || head[1] != 0x00) return -1;
    size_t skip = 0;
    switch (head[3]) {
    case 0x01: skip = 4; break;
    case 0x03: {
        unsigned char alen = 0;
        if (xrecv_exact(fd, &alen, 1) != 0) return -1;
        skip = alen;
        break;
    }
    case 0x04: skip = 16; break;
    default: return -1;
    }
    unsigned char tail[2];
    if (skip > 0) {
        unsigned char junk[16];
        size_t left = skip;
        while (left > 0) {
            size_t chunk = left > sizeof(junk) ? sizeof(junk) : left;
            if (xrecv_exact(fd, junk, chunk) != 0) return -1;
            left -= chunk;
        }
    }
    if (xrecv_exact(fd, tail, 2) != 0) return -1;
    return 0;
}

/* ------------------------------------------------------------- hooks */

int connect(int fd, const struct sockaddr *addr, socklen_t len) {
    resolve_real_fns();
    if (real_connect_fn == NULL) {
        errno = ENOSYS;
        return -1;
    }
    load_config();
    if (!g_cfg.enabled || addr == NULL || addr->sa_family != AF_INET) {
        return real_connect_fn(fd, addr, len);
    }

    const struct sockaddr_in *sin = (const struct sockaddr_in *)addr;
    in_addr_t dst_ip = sin->sin_addr.s_addr;
    unsigned short dst_port = ntohs(sin->sin_port);

    /* Proxy endpoint itself: straight through, never filtered or tunneled. */
    struct in_addr proxy_addr;
    if (inet_pton(AF_INET, g_cfg.proxy_ip, &proxy_addr) == 1 &&
        proxy_addr.s_addr == dst_ip &&
        dst_port == g_cfg.proxy_port) {
        return real_connect_fn(fd, addr, len);
    }

    char host[MAX_HOSTNAME + 1] = "";
    const char *hostname = NULL;
    if (in_synth_range(dst_ip) &&
        host_for_ip(dst_ip, host, sizeof(host)) == 0) {
        hostname = host;
    }
    if (g_debug)
        log_msg("egress-proxy: connect dst=%s:%u host=%s",
                inet_ntoa(sin->sin_addr), dst_port,
                hostname != NULL ? hostname : "(none)");
    if (!filter_allows(hostname, dst_ip)) {
        if (g_debug) log_msg("egress-proxy: filtered out");
        errno = ECONNREFUSED;
        return -1;
    }
    if (socks5_connect(fd, hostname, dst_ip, dst_port) != 0) {
        errno = ECONNREFUSED;
        return -1;
    }
    return 0;
}

int getaddrinfo(const char *node, const char *service,
                const struct addrinfo *hints, struct addrinfo **res) {
    resolve_real_fns();
    if (real_getaddrinfo_fn == NULL) return EAI_SYSTEM;
    load_config();

    if (!g_cfg.enabled || node == NULL || *node == '\0') {
        return real_getaddrinfo_fn(node, service, hints, res);
    }
    struct in_addr literal;
    if (inet_pton(AF_INET, node, &literal) == 1) {
        return real_getaddrinfo_fn(node, service, hints, res);
    }
    if (strlen(node) > MAX_HOSTNAME) return EAI_NONAME;

    unsigned long port = 80;
    if (service != NULL) port = strtoul(service, NULL, 10);
    if (port == 0 || port > 65535) return EAI_NONAME;

    struct addrinfo *ai = calloc(1, sizeof(*ai));
    struct sockaddr_in *sa = calloc(1, sizeof(*sa));
    if (ai == NULL || sa == NULL) {
        free(ai);
        free(sa);
        return EAI_MEMORY;
    }
    in_addr_t synth = synth_for_host(node);
    sa->sin_family = AF_INET;
    sa->sin_port = htons((unsigned short)port);
    sa->sin_addr.s_addr = synth;
    ai->ai_family = AF_INET;
    ai->ai_socktype = hints != NULL && hints->ai_socktype ? hints->ai_socktype
                                                          : SOCK_STREAM;
    ai->ai_protocol = hints != NULL && hints->ai_protocol ? hints->ai_protocol
                                                          : IPPROTO_TCP;
    ai->ai_addrlen = sizeof(*sa);
    ai->ai_addr = (struct sockaddr *)sa;
    ai->ai_next = NULL;
    *res = ai;
    return 0;
}
