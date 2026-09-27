#include <Arduino.h>
#include "dshot.h"
#include "dshot_output.h"
#if CORVIDIA_DSHOT_BOUNDED
#include <HardwareTimer.h>
#include "dshot_test.h"
#endif

#if !defined(STM32F405xx)
#error "DShot diagnostic DMA mapping is specific to STM32F405"
#endif

namespace {
// STM32F405: TIM3_UP DMA1 stream 2/channel 5; TIM4_UP stream 6/channel 2.
// Non-circular transfers cannot replay a stale throttle frame indefinitely.
alignas(4) Dshot::PairBuffer tim3Data{}, tim4Data{};
volatile bool initialized = false, failed = false;
bool inFlight = false;
uint32_t launchedAt = 0;
volatile uint32_t completed = 0;
Dshot::Timing timing;
#if CORVIDIA_DSHOT_BOUNDED
HardwareTimer frameTimer(TIM5);
DshotLease lease;
uint32_t lock() { const auto p = __get_PRIMASK(); __disable_irq(); return p; }
void unlock(uint32_t p) { __DMB(); if (!p) __enable_irq(); }
void frameISR();
#endif
constexpr uint32_t FRAME_INTERVAL_US = 1000;
constexpr uint32_t FRAME_COMPLETE_US = 80; // 18 DMA slots + preload pipeline
constexpr uint32_t TRANSFER_TIMEOUT_US = 200;
constexpr uint32_t STREAM2_FLAGS = DMA_LISR_FEIF2 | DMA_LISR_DMEIF2 |
    DMA_LISR_TEIF2 | DMA_LISR_HTIF2 | DMA_LISR_TCIF2;
constexpr uint32_t STREAM6_FLAGS = DMA_HISR_FEIF6 | DMA_HISR_DMEIF6 |
    DMA_HISR_TEIF6 | DMA_HISR_HTIF6 | DMA_HISR_TCIF6;
constexpr uint32_t STREAM2_ERRORS = DMA_LISR_FEIF2 | DMA_LISR_DMEIF2 | DMA_LISR_TEIF2;
constexpr uint32_t STREAM6_ERRORS = DMA_HISR_FEIF6 | DMA_HISR_DMEIF6 | DMA_HISR_TEIF6;

void pinsLow() {
    __HAL_RCC_GPIOB_CLK_ENABLE();
    __HAL_RCC_GPIOC_CLK_ENABLE();
    GPIOB->BSRR = (GPIO_PIN_8 | GPIO_PIN_9) << 16;
    GPIOC->BSRR = (GPIO_PIN_6 | GPIO_PIN_7) << 16;
    GPIO_InitTypeDef g{};
    g.Mode = GPIO_MODE_OUTPUT_PP;
    g.Pull = GPIO_PULLDOWN;
    g.Speed = GPIO_SPEED_FREQ_VERY_HIGH;
    g.Pin = GPIO_PIN_8 | GPIO_PIN_9; HAL_GPIO_Init(GPIOB, &g);
    g.Pin = GPIO_PIN_6 | GPIO_PIN_7; HAL_GPIO_Init(GPIOC, &g);
}
bool disableStream(DMA_Stream_TypeDef* stream) {
    stream->CR &= ~DMA_SxCR_EN;
    // Bounded wait; no dependency on SysTick or a DMA interrupt.
    for (unsigned n = 0; n < 10000; ++n)
        if (!(stream->CR & DMA_SxCR_EN)) return true;
    return false;
}
void halt(bool fault) {
    TIM3->DIER = TIM4->DIER = 0;
    TIM3->CR1 &= ~TIM_CR1_CEN; TIM4->CR1 &= ~TIM_CR1_CEN;
    TIM3->CCER = TIM4->CCER = 0;
    const bool a = disableStream(DMA1_Stream2);
    const bool b = disableStream(DMA1_Stream6);
    pinsLow();
    failed = failed || fault || !a || !b;
    initialized = inFlight = false;
}
void configureTimer(TIM_TypeDef* timer, const Dshot::Timing& t, bool upper) {
    timer->CR1 = TIM_CR1_ARPE;
    timer->CR2 = timer->SMCR = timer->DIER = timer->CCER = 0;
    timer->PSC = 0;
    timer->ARR = t.period - 1;
    timer->CNT = 0;
    timer->CCR1 = timer->CCR2 = timer->CCR3 = timer->CCR4 = 0;
    // PWM mode 1 and compare preload for each channel of the selected pair.
    const uint32_t mode = (6u << 4) | TIM_CCMR1_OC1PE |
                          (6u << 12) | TIM_CCMR1_OC2PE;
    timer->CCMR1 = upper ? 0 : mode;
    timer->CCMR2 = upper ? mode : 0;
    timer->DCR = (upper ? TIM_DMABASE_CCR3 : TIM_DMABASE_CCR1) |
                 TIM_DMABURSTLENGTH_2TRANSFERS;
    timer->EGR = TIM_EGR_UG;
    timer->SR = 0;
    timer->CCER = upper ? (TIM_CCER_CC3E | TIM_CCER_CC4E) :
                          (TIM_CCER_CC1E | TIM_CCER_CC2E);
}
void configureStream(DMA_Stream_TypeDef* stream, unsigned channel,
                     TIM_TypeDef* timer, Dshot::PairBuffer& data) {
    stream->CR = (channel << DMA_SxCR_CHSEL_Pos) | DMA_SxCR_PL_1 |
        DMA_SxCR_MSIZE_0 | DMA_SxCR_PSIZE_0 | DMA_SxCR_MINC | DMA_SxCR_DIR_0;
    // Halfword transfers, memory increment, fixed peripheral DMAR, normal mode.
    stream->FCR = 0;
    stream->PAR = reinterpret_cast<uint32_t>(&timer->DMAR);
    stream->M0AR = reinterpret_cast<uint32_t>(data.data());
    stream->NDTR = data.size();
}
void launch(uint32_t now) {
    TIM3->DIER = TIM4->DIER = 0;
    TIM3->CR1 &= ~TIM_CR1_CEN; TIM4->CR1 &= ~TIM_CR1_CEN;
    if (!disableStream(DMA1_Stream2) || !disableStream(DMA1_Stream6)) {
        halt(true); return;
    }
#if CORVIDIA_DSHOT_BOUNDED
    // DMA has finished before the buffers are rewritten. Main never edits them.
    if (!Dshot::makePair(lease.value(3), lease.value(4), timing, tim3Data) ||
        !Dshot::makePair(lease.value(1), lease.value(2), timing, tim4Data)) {
        halt(true); return;
    }
#endif
    // Establish low active and preloaded CCRs before the first DMA update.
    TIM3->CCR1 = TIM3->CCR2 = TIM4->CCR3 = TIM4->CCR4 = 0;
    TIM3->CNT = TIM4->CNT = 0;
    TIM3->EGR = TIM_EGR_UG; TIM4->EGR = TIM_EGR_UG;
    TIM3->SR = TIM4->SR = 0;
    DMA1->LIFCR = STREAM2_FLAGS; DMA1->HIFCR = STREAM6_FLAGS;
    DMA1_Stream2->M0AR = reinterpret_cast<uint32_t>(tim3Data.data());
    DMA1_Stream6->M0AR = reinterpret_cast<uint32_t>(tim4Data.data());
    DMA1_Stream2->NDTR = tim3Data.size();
    DMA1_Stream6->NDTR = tim4Data.size();
    __DMB();
    DMA1_Stream2->CR |= DMA_SxCR_EN; DMA1_Stream6->CR |= DMA_SxCR_EN;
    TIM3->DIER = TIM_DIER_UDE; TIM4->DIER = TIM_DIER_UDE;
    launchedAt = now;
    inFlight = true;
    TIM3->CR1 |= TIM_CR1_CEN; TIM4->CR1 |= TIM_CR1_CEN;
}
}

bool DshotOutput::begin() {
    // Runtime interlock also exercises compilation of the complete driver.
    if (initialized || failed) return false;
    pinsLow();
    if (!CORVIDIA_DSHOT_SIGNALS) return false;
    __HAL_RCC_DMA1_CLK_ENABLE();
    __HAL_RCC_TIM3_CLK_ENABLE(); __HAL_RCC_TIM4_CLK_ENABLE();
    if ((DMA1_Stream2->CR & DMA_SxCR_EN) || (DMA1_Stream6->CR & DMA_SxCR_EN)) {
        failed = true; return false; // a different peripheral already owns DMA
    }
    __HAL_RCC_TIM3_FORCE_RESET(); __HAL_RCC_TIM3_RELEASE_RESET();
    __HAL_RCC_TIM4_FORCE_RESET(); __HAL_RCC_TIM4_RELEASE_RESET();
    uint32_t timerClock = HAL_RCC_GetPCLK1Freq();
    if ((RCC->CFGR & RCC_CFGR_PPRE1) != 0) timerClock *= 2;
    auto& t = timing;
    if (!Dshot::timing(timerClock, t) || !Dshot::makePair(0, 0, t, tim3Data) ||
        !Dshot::makePair(0, 0, t, tim4Data)) { failed = true; return false; }
    configureTimer(TIM3, t, false); configureTimer(TIM4, t, true);
    configureStream(DMA1_Stream2, 5, TIM3, tim3Data);
    configureStream(DMA1_Stream6, 2, TIM4, tim4Data);
    GPIO_InitTypeDef g{};
    g.Mode = GPIO_MODE_AF_PP; g.Pull = GPIO_PULLDOWN;
    g.Speed = GPIO_SPEED_FREQ_VERY_HIGH; g.Alternate = GPIO_AF2_TIM4;
    g.Pin = GPIO_PIN_8 | GPIO_PIN_9; HAL_GPIO_Init(GPIOB, &g);
    g.Alternate = GPIO_AF2_TIM3;
    g.Pin = GPIO_PIN_6 | GPIO_PIN_7; HAL_GPIO_Init(GPIOC, &g);
    initialized = true;
    launchedAt = micros() - FRAME_INTERVAL_US;
#if CORVIDIA_DSHOT_BOUNDED
    frameTimer.setOverflow(FRAME_INTERVAL_US, MICROSEC_FORMAT);
    frameTimer.attachInterrupt(frameISR);
    frameTimer.setInterruptPriority(1, 0);
    frameTimer.resume();
#endif
    return true;
}
namespace {
void serviceFrames(uint32_t now) {
    if (!initialized) return;
    if ((DMA1->LISR & STREAM2_ERRORS) || (DMA1->HISR & STREAM6_ERRORS)) {
        halt(true); return;
    }
    const uint32_t age = now - launchedAt;
    if (inFlight) {
        const bool finished = (DMA1->LISR & DMA_LISR_TCIF2) &&
                              (DMA1->HISR & DMA_HISR_TCIF6);
        if (finished && age >= FRAME_COMPLETE_US) {
            inFlight = false; ++completed;
        } else if (age >= TRANSFER_TIMEOUT_US) {
            halt(true); return;
        }
    }
    if (!inFlight && age >= FRAME_INTERVAL_US) launch(now);
}
#if CORVIDIA_DSHOT_BOUNDED
void frameISR() {
    lease.tick(millis());
    serviceFrames(micros());
}
#endif
}
void DshotOutput::service(uint32_t now) {
#if !CORVIDIA_DSHOT_BOUNDED
    serviceFrames(now);
#else
    (void)now; // TIM5 owns frame transmission in the integrated image.
#endif
}
void DshotOutput::disable() {
#if CORVIDIA_DSHOT_BOUNDED
    const auto p = lock(); frameTimer.pause(); lease.stop();
#endif
    if (initialized) halt(false); else pinsLow();
#if CORVIDIA_DSHOT_BOUNDED
    unlock(p);
#endif
}
bool DshotOutput::ready() { return initialized; }
bool DshotOutput::faulted() { return failed; }
uint32_t DshotOutput::frames() { return completed; }
#if CORVIDIA_DSHOT_BOUNDED
bool DshotOutput::motorTestsAllowed() { return CORVIDIA_DSHOT_MOTOR_TESTS && ready() && !faulted(); }
bool DshotOutput::start(uint8_t motor, uint16_t value, uint32_t durationMs,
                        uint32_t attitudeAt, uint32_t gyroAt, uint32_t heartbeatAt,
                        const DshotLimits& limits) {
    const auto p = lock();
    const bool ok = motorTestsAllowed() && lease.start(millis(), motor, value, durationMs,
                                                      attitudeAt, gyroAt, heartbeatAt, limits,
                                                      CORVIDIA_DSHOT_RAMP_MS);
    unlock(p); return ok;
}
void DshotOutput::refresh(uint32_t attitudeAt, uint32_t gyroAt, uint32_t heartbeatAt) {
    const auto p = lock(); lease.refresh(millis(), attitudeAt, gyroAt, heartbeatAt); unlock(p);
}
void DshotOutput::stop() { const auto p = lock(); lease.stop(); unlock(p); }
bool DshotOutput::expired() { const auto p = lock(); const bool v = lease.expired(); unlock(p); return v; }
uint16_t DshotOutput::commanded(uint8_t motor) {
    const auto p = lock(); const auto v = lease.value(motor); unlock(p); return v;
}
#endif
