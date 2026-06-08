#include "Audio.h"
#include <ArduinoJson.h>
#include <ESP_I2S.h>
#include <HTTPClient.h>
#include <WiFi.h>
#include <esp_wifi.h>
#include <Adafruit_NeoPixel.h>

// ==========================================
// НАСТРОЙКИ СЕТИ И СЕРВЕРА
// ==========================================
const char *ssid = "realmeRZ";
const char *password = "zxcvbnmz";

// IP-адрес вашего локального Python-сервера (без http://)
const char *SERVER_IP = "89.22.225.178";
// Порт вашего локального Python-сервера
const int SERVER_PORT = 8000;

// ==========================================
// ПИНЫ И НАСТРОЙКИ ОБОРУДОВАНИЯ
// ==========================================
// Пин кнопки (на многих ESP32-платах это GPIO0)
#define BOOT_BUTTON_PIN 0

// Пины для I2S динамика (MAX98357A или аналог)
#define I2S_DOUT 47
#define I2S_BCLK 21 // Изменили с 48 на 21, так как он физически находится рядом с 47 пином
#define I2S_LRC 45

// Пины для I2S микрофона (INMP441)
#define I2S_MIC_SERIAL_CLOCK 5     // SCK
#define I2S_MIC_LEFT_RIGHT_CLOCK 4 // WS
#define I2S_MIC_SERIAL_DATA 6      // SD

// Настройки аудио для записи
#define SAMPLE_RATE 8000
const int MIC_BUFFER_SIZE = 512;

// ==========================================
// НАСТРОЙКИ СВЕТОДИОДА (WS2812B)
// ==========================================
// Адресный светодиод на плате ESP32-S3
#define LED_PIN 48 
#define NUMPIXELS 1
Adafruit_NeoPixel pixels(NUMPIXELS, LED_PIN, NEO_GRB + NEO_KHZ800);

// Глобальные объекты
Audio audio;
I2SClass i2sMic;
std::vector<int16_t> audioBuffer;

// Прототип функции отправки логов
void sendLogToServer(String level, String message);

// Таймер для периодической отправки логов
unsigned long lastLogTime = 0;
const unsigned long LOG_INTERVAL = 90000; // каждые 90 секунд

// Состояние кнопки
bool buttonPressed = false;
bool wasButtonPressed = false;
bool isRecording = false;

// ==========================================
// ФУНКЦИИ
// ==========================================

void setup() {
  Serial.begin(115200);
  delay(1000);
  Serial.println("\n\n----- Local Voice Assistant Terminal -----");

  // Инициализация светодиода
  pixels.begin();
  pixels.setPixelColor(0, pixels.Color(0, 0, 0)); // Выключен по умолчанию
  pixels.show();

  // Настройка кнопки
  pinMode(BOOT_BUTTON_PIN, INPUT_PULLUP);

  // Настройка Wi-Fi
  WiFi.mode(WIFI_STA);
  WiFi.begin(ssid, password);
  Serial.print("Connecting to WiFi");
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }
  Serial.println("\nWiFi connected. IP: " + WiFi.localIP().toString());

  // ОТКЛЮЧАЕМ ЭНЕРГОСБЕРЕЖЕНИЕ WIFI ДЛЯ МАКСИМАЛЬНОЙ СКОРОСТИ ОТДАЧИ
  esp_wifi_set_ps(WIFI_PS_NONE);

  // Настройка I2S для воспроизведения звука (класс Audio)
  audio.setPinout(I2S_BCLK, I2S_LRC, I2S_DOUT);
  audio.setVolume(21); // Громкость (0-21)

  // Настройка I2S для записи (микрофон INMP441)
  i2sMic.setPins(I2S_MIC_SERIAL_CLOCK, I2S_MIC_LEFT_RIGHT_CLOCK, -1,
                 I2S_MIC_SERIAL_DATA);
  if (!i2sMic.begin(I2S_MODE_STD, SAMPLE_RATE, I2S_DATA_BIT_WIDTH_16BIT,
                    I2S_SLOT_MODE_MONO, I2S_STD_SLOT_LEFT)) {
    Serial.println("ОШИБКА: Не удалось инициализировать I2S микрофон!");
  } else {
    Serial.println("I2S микрофон успешно инициализирован.");
  }

  // Резервируем память для буфера (если есть PSRAM, vector будет использовать
  // ее при правильной настройке ядра)
  audioBuffer.reserve(SAMPLE_RATE * 5); // Резерв на 5 секунд

  Serial.println("\n----- Система готова -----");
  Serial.println("Удерживайте кнопку BOOT для записи, отпустите для отправки.");
  
  sendLogToServer("INFO", "Устройство успешно включено. IP: " + WiFi.localIP().toString());
}

void loop() {
  // 1. Обязательный вызов loop для класса Audio (отвечает за воспроизведение
  // потока)
  audio.loop();

  // 2. Чтение состояния кнопки
  buttonPressed = (digitalRead(BOOT_BUTTON_PIN) == LOW);

  // Обработка логики Push-to-talk
  if (buttonPressed && !wasButtonPressed && !isRecording) {
    // Кнопка только что нажата - начинаем запись
    Serial.println("\n----- Начало записи -----");
    audioBuffer.clear();

    pixels.setPixelColor(0, pixels.Color(255, 0, 0)); // Красный = Слушаю
    pixels.show();

    // Останавливаем воспроизведение, если оно идет
    audio.stopSong();

    isRecording = true;
    wasButtonPressed = true;
  } else if (buttonPressed && isRecording) {
    // Кнопка удерживается - читаем данные с микрофона
    int16_t samples[MIC_BUFFER_SIZE];
    size_t bytesRead =
        i2sMic.readBytes((char *)samples, MIC_BUFFER_SIZE * sizeof(int16_t));

    if (bytesRead > 0) {
      size_t samplesRead = bytesRead / sizeof(int16_t);
      for (size_t i = 0; i < samplesRead; i++) {
        audioBuffer.push_back(samples[i]);
      }
    }
  } else if (!buttonPressed && wasButtonPressed && isRecording) {
    // Кнопка отпущена - останавливаем запись и отправляем данные
    Serial.println("\n----- Запись остановлена -----");
    isRecording = false;
    wasButtonPressed = false;

    if (audioBuffer.size() > 0) {
      Serial.printf("Записано %d сэмплов. Отправка на сервер...\n",
                    audioBuffer.size());
      
      sendLogToServer("INFO", "Отправка голосового запроса на сервер (" + String(audioBuffer.size() * sizeof(int16_t)) + " байт)");
                    
      pixels.setPixelColor(0, pixels.Color(0, 0, 255)); // Синий = Обработка на сервере
      pixels.show();
      
      sendAudioToServer();
    } else {
      Serial.println("Буфер пуст, ничего не записано.");
      pixels.setPixelColor(0, pixels.Color(0, 0, 0)); // Выключаем
      pixels.show();
    }
  }

  // Выключаем светодиод, если песня закончилась
  static bool wasRunning = false;
  bool currentlyRunning = audio.isRunning();
  if (wasRunning && !currentlyRunning && !isRecording) {
      pixels.setPixelColor(0, pixels.Color(0, 0, 0)); // Выключаем после завершения речи
      pixels.show();
  }
  wasRunning = currentlyRunning;

  // Периодическая отправка логов о состоянии микроконтроллера (каждые 30 сек)
  if (millis() - lastLogTime > LOG_INTERVAL) {
    lastLogTime = millis();
    String heapMsg = "Устройство работает. Свободная память (Heap): " + String(ESP.getFreeHeap()) + " байт";
    sendLogToServer("INFO", heapMsg);
  }

  // Небольшая задержка, чтобы не перегружать цикл, когда ничего не происходит
  if (!currentlyRunning && !isRecording) {
    delay(10);
  }
}

// Отправка сырого PCM аудио на Python-сервер и получение ответа
void sendAudioToServer() {
  HTTPClient http;
  String url =
      String("http://") + SERVER_IP + ":" + SERVER_PORT + "/api/process_audio";

  Serial.println("Подключение к: " + url);
  http.begin(url);
  http.setTimeout(30000); // ждём ответ от сервера до 30 сек

  // Отправляем как бинарные данные (сырой PCM)
  http.addHeader("Content-Type", "application/octet-stream");

  // Рассчитываем размер данных в байтах
  size_t dataSize = audioBuffer.size() * sizeof(int16_t);

  // Выполняем POST запрос
  int httpResponseCode = http.POST((uint8_t *)audioBuffer.data(), dataSize);

  if (httpResponseCode == 200) {
    // Успешный ответ
    String response = http.getString();
    Serial.println("Ответ сервера: " + response);

    // Ожидаем, что сервер ответит JSON'ом вида: {"url":
    // "http://192.168.1.100:8000/audio/reply.mp3"}
    DynamicJsonDocument doc(1024);
    DeserializationError error = deserializeJson(doc, response);

    if (!error) {
      const char *audioUrl = doc["url"];
      if (audioUrl) {
        Serial.println("Получен URL для воспроизведения: " + String(audioUrl));
        sendLogToServer("INFO", "Успешный ответ от сервера. Воспроизведение аудио...");

        // Перенастраиваем пины для динамика перед воспроизведением
        // audio.setPinout(I2S_BCLK, I2S_LRC, I2S_DOUT);

        // Передаем URL в класс Audio
        if (audio.connecttohost(audioUrl)) {
          Serial.println("Начато воспроизведение ответа.");
          pixels.setPixelColor(0, pixels.Color(0, 255, 0)); // Зеленый = Говорю
          pixels.show();
        } else {
          Serial.println("ОШИБКА: Не удалось подключиться к аудиопотоку!");
          pixels.setPixelColor(0, pixels.Color(255, 0, 0)); // Ошибка - красный
          pixels.show();
        }
      } else {
        Serial.println("ОШИБКА: URL не найден в JSON ответе.");
      }
    } else {
      Serial.println("ОШИБКА: Не удалось распарсить JSON.");
    }
  } else {
    String errorMsg = "HTTP POST аудио провалился. Код: " + String(httpResponseCode);
    Serial.println("ОШИБКА: " + errorMsg);
    sendLogToServer("ERROR", errorMsg);
  }

  http.end();

  // Очищаем буфер для следующей записи
  audioBuffer.clear();
}

// Отправка текстовых логов на Python-сервер
void sendLogToServer(String level, String message) {
  if (WiFi.status() == WL_CONNECTED) {
    HTTPClient http;
    String url = String("http://") + SERVER_IP + ":" + SERVER_PORT + "/api/log";
    http.begin(url);
    http.addHeader("Content-Type", "application/json");

    DynamicJsonDocument doc(256);
    doc["level"] = level;
    doc["message"] = message;
    
    String requestBody;
    serializeJson(doc, requestBody);
    
    int httpResponseCode = http.POST(requestBody);
    if (httpResponseCode <= 0) {
      Serial.println("Ошибка отправки лога: " + String(httpResponseCode));
    }
    http.end();
  }
}
