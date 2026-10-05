/* Thin speech↔RADE V1 wrapper for CyberRig. Built against rade_c. */

#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <stdint.h>

#include "rade_api.h"
#include "rade_dsp.h"
#include "lpcnet.h"
#include "fargan.h"
#include "cpu_support.h"

#define FEAT RADE_NB_TOTAL_FEATURES  /* 36 */
#define NB_FEAT 20

typedef struct {
    struct rade *r;
    LPCNetEncState *enc;
    FARGANState fargan;
    int arch;
    int fargan_ready;
    int cont_frames;
    float cont_buf[5 * FEAT];
    float features[512];
    int feat_idx;
    int frames_per_mf;
    int n_tx;
    int n_eoo;
    int n_features;
} rs_t;

static float clampf(float v) {
    if (v > 32767.f) return 32767.f;
    if (v < -32767.f) return -32767.f;
    return v;
}

void *rs_open(void) {
    rade_initialize();
    rs_t *s = calloc(1, sizeof(*s));
    if (!s) return NULL;
    int flags = RADE_USE_C_ENCODER | RADE_USE_C_DECODER | RADE_VERBOSE_0;
    s->r = rade_open("", flags);
    if (!s->r) {
        free(s);
        return NULL;
    }
    s->arch = opus_select_arch();
    s->enc = lpcnet_encoder_create();
    fargan_init(&s->fargan);
    s->n_features = rade_n_features_in_out(s->r);
    s->n_tx = rade_n_tx_out(s->r);
    s->n_eoo = rade_n_tx_eoo_out(s->r);
    s->frames_per_mf = s->n_features / FEAT;
    if (s->frames_per_mf < 1) s->frames_per_mf = 1;
    return s;
}

void rs_close(void *h) {
    rs_t *s = h;
    if (!s) return;
    if (s->enc) lpcnet_encoder_destroy(s->enc);
    if (s->r) rade_close(s->r);
    rade_finalize();
    free(s);
}

int rs_n_tx(void *h) { return h ? ((rs_t *)h)->n_tx : 0; }
int rs_n_eoo(void *h) { return h ? ((rs_t *)h)->n_eoo : 0; }
int rs_sync(void *h) { return h ? rade_sync(((rs_t *)h)->r) : 0; }
float rs_snr(void *h) {
    rs_t *s = h;
    if (!s || !rade_sync(s->r)) return 0.f;
    return rade_snrdB_3k_est(s->r);
}

/* Push 160 samples of 16 kHz int16 speech. When a modem frame is ready,
   writes n_tx real samples into out[] and returns that count; else 0. */
int rs_tx_frame(void *h, const int16_t *pcm160, float *out) {
    rs_t *s = h;
    if (!s || !pcm160 || !out) return 0;
    lpcnet_compute_single_frame_features(
        s->enc, pcm160, &s->features[s->feat_idx * FEAT], s->arch);
    s->feat_idx++;
    if (s->feat_idx < s->frames_per_mf) return 0;
    RADE_COMP *iq = malloc((size_t)s->n_tx * sizeof(RADE_COMP));
    if (!iq) { s->feat_idx = 0; return 0; }
    int n = rade_tx(s->r, iq, s->features);
    for (int i = 0; i < n; i++) {
        float v = iq[i].real * RADE_INT16_SCALE;
        out[i] = clampf(v) / 32768.f;
    }
    free(iq);
    s->feat_idx = 0;
    return n;
}

int rs_tx_eoo(void *h, float *out) {
    rs_t *s = h;
    if (!s || !out) return 0;
    RADE_COMP *iq = malloc((size_t)s->n_eoo * sizeof(RADE_COMP));
    if (!iq) return 0;
    int n = rade_tx_eoo(s->r, iq);
    for (int i = 0; i < n; i++) {
        float v = iq[i].real * RADE_INT16_SCALE;
        out[i] = clampf(v) / 32768.f;
    }
    free(iq);
    return n;
}

/* Feed nin 8 kHz real samples (SSB). Writes 16 kHz float speech into out.
   Returns number of speech samples (multiple of 160). Caller must pass
   exactly rade_nin() samples; we expose rs_nin(). */
int rs_nin(void *h) { return h ? rade_nin(((rs_t *)h)->r) : 0; }
int rs_nin_max(void *h) { return h ? rade_nin_max(((rs_t *)h)->r) : 0; }

int rs_rx_block(void *h, const float *modem, int nin, float *speech_out, int max_out) {
    rs_t *s = h;
    if (!s || !modem || nin <= 0) return 0;
    RADE_COMP *iq = malloc((size_t)nin * sizeof(RADE_COMP));
    if (!iq) return 0;
    for (int i = 0; i < nin; i++) {
        iq[i].real = modem[i] * 2.f; /* real-SSB scale, see rade_api.h */
        iq[i].imag = 0.f;
    }
    float *feat = malloc((size_t)s->n_features * sizeof(float));
    int has_eoo = 0;
    int n_eoo_bits = rade_n_eoo_bits(s->r);
    float *eoo = n_eoo_bits ? malloc((size_t)n_eoo_bits * sizeof(float)) : NULL;
    int nout = rade_rx(s->r, feat, &has_eoo, eoo, iq);
    free(eoo);
    free(iq);
    if (nout <= 0) { free(feat); return 0; }

    int n_frames = nout / FEAT;
    int written = 0;
    for (int fi = 0; fi < n_frames; fi++) {
        float *f = &feat[fi * FEAT];
        if (!s->fargan_ready) {
            memcpy(&s->cont_buf[s->cont_frames * FEAT], f, FEAT * sizeof(float));
            if (++s->cont_frames >= 5) {
                float packed[5 * NB_FEAT];
                for (int i = 0; i < 5; i++)
                    memcpy(&packed[i * NB_FEAT],
                           &s->cont_buf[i * FEAT],
                           NB_FEAT * sizeof(float));
                float zeros[FARGAN_CONT_SAMPLES];
                memset(zeros, 0, sizeof(zeros));
                fargan_cont(&s->fargan, zeros, packed);
                s->fargan_ready = 1;
            }
            continue;
        }
        if (written + LPCNET_FRAME_SIZE > max_out) break;
        fargan_synthesize(&s->fargan, speech_out + written, f);
        written += LPCNET_FRAME_SIZE;
    }
    free(feat);
    return written;
}
