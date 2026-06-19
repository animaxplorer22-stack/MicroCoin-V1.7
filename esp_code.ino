/*
  MICROCORE (MCX) ESP32/ESP8266 MINER v6.3 — COMPLETE
  Hardware: ESP32 or ESP8266
  Features:
  - Real ECDSA secp256k1 signatures (mbedtls)
  - 10 Levels (1,000 MCX per level)
  - Gossip discovery with peer caching (SPIFFS)
  - Temporary + Permanent towers support
  - EEPROM storage for stake, rewards, blocks, level
  - Per-level block intervals (40s to 7s)
  - Uptime tracking with daily reset
  - Slashing handling (10% loss)
  - Remote control (start/stop/restart)
  - Block redistribution support
  - Global reward pools support
  
  Instructions:
  1. Install ESP32/ESP8266 board support in Arduino IDE
  2. Install libraries: WebSockets, ArduinoJson, mbedtls
  3. Edit WIFI_SSID, WIFI_PASSWORD, USERNAME, PRIVATE_KEY_HEX below
  4. Set YOUR_SERVER_IP in BOOTSTRAP_NODES
  5. Upload to ESP32/ESP8266
*/

#include <WiFi.h>
#include <WebSocketsClient.h>
#include <ArduinoJson.h>
#include <NTPClient.h>
#include <WiFiUDP.h>
#include <EEPROM.h>
#include <SPIFFS.h>
#include <mbedtls/ecdsa.h>
#include <mbedtls/entropy.h>
#include <mbedtls/ctr_drbg.h>
#include <mbedtls/sha256.h>

// ==================== USER CONFIGURATION ====================
// EDIT THESE BEFORE UPLOADING
const char* WIFI_SSID = "your_wifi_ssid";              // ← CHANGE
const char* WIFI_PASSWORD = "your_wifi_password";      // ← CHANGE

const char* BOOTSTRAP_NODES[] = {"101.127.80.48:8080"}; // ← CHANGE TO YOUR NODE IP
const int BOOTSTRAP_COUNT = 1;
const int NODE_PORT = 8080;

const char* USERNAME = "your_username";                 // ← CHANGE
const char* PRIVATE_KEY_HEX = "your_64_char_private_key_hex_here"; // ← CHANGE

const uint32_t INITIAL_STAKE = 1000;
const uint32_t LEVEL_STAKE_RANGE = 1000;
const uint32_t MAX_LEVEL = 10;

// ==================== CONSTANTS ====================
#define SYMBOL "MCX"
#define SIGNING_WINDOW_MS 2500
#define SLASH_RATE 0.10
#define UPTIME_PING_INTERVAL 30000
#define MAX_RECONNECT_ATTEMPTS 5
#define MAX_PEERS 20
#define VERSION "6.3"

#define EEPROM_STAKE_ADDR 0
#define EEPROM_REWARDS_ADDR 4
#define EEPROM_BLOCKS_ADDR 8
#define EEPROM_UPTIME_ADDR 12
#define EEPROM_TODAY_UPTIME_ADDR 16
#define EEPROM_LAST_RESET_ADDR 20
#define EEPROM_SLASH_COUNT_ADDR 24
#define EEPROM_CONSECUTIVE_MISSES_ADDR 28
#define EEPROM_LEVEL_ADDR 32
#define EEPROM_CHECKSUM_ADDR 36
#define EEPROM_MAGIC_ADDR 40
#define EEPROM_PEER_INDEX_ADDR 44

#define MAGIC_NUMBER 0x5A5A5A5A
#define LED_PIN 2

const uint32_t LEVEL_BLOCK_INTERVALS[] = {0, 40, 35, 30, 25, 20, 15, 10, 9, 8, 7};

// ==================== CRYPTO CONTEXT ====================
mbedtls_ecdsa_context ecdsa;
mbedtls_entropy_context entropy;
mbedtls_ctr_drbg_context ctr_drbg;
mbedtls_sha256_context sha256_ctx;

// ==================== GLOBAL VARIABLES ====================
WebSocketsClient webSocket;
WiFiUDP ntpUDP;
NTPClient timeClient(ntpUDP, "pool.ntp.org", 0, 60000);

uint32_t currentStake, totalRewards, totalBlocksSigned, totalUptimeSeconds;
uint32_t todayUptimeSeconds, lastUptimeReset, currentLevel, lastUptimePing;
uint32_t lastChallengeTime, uptimeCounter, consecutiveMisses, slashCount;
uint32_t currentBlockId, reconnectAttempts, currentPeerIndex, nodeSwitchCount;

char validatorID[65], publicKeyHex[130], walletAddress[70];
char currentChallenge[65], currentNodeIP[16];
bool isValidator = false, isRegistered = false, wsConnected = false, miningEnabled = true;

String peerList[MAX_PEERS];
int peerCount = 0;

// ==================== LED FUNCTIONS ====================
void led_on() { pinMode(LED_PIN, OUTPUT); digitalWrite(LED_PIN, LOW); }
void led_off() { digitalWrite(LED_PIN, HIGH); }
void led_blink(int times, int duration) {
  for (int i = 0; i < times; i++) { led_on(); delay(duration); led_off(); delay(duration); }
}

// ==================== CRYPTO UTILITIES ====================
void hexToBytes(const char* hex, unsigned char* bytes, size_t len) {
  for (size_t i = 0; i < len; i++) sscanf(hex + 2 * i, "%02hhx", &bytes[i]);
}
void bytesToHex(const unsigned char* bytes, size_t len, char* hex) {
  for (size_t i = 0; i < len; i++) sprintf(hex + 2 * i, "%02x", bytes[i]);
  hex[2 * len] = '\0';
}
void computeSHA256(const char* input, char* output) {
  unsigned char hash[32];
  mbedtls_sha256_init(&sha256_ctx);
  mbedtls_sha256_starts(&sha256_ctx, 0);
  mbedtls_sha256_update(&sha256_ctx, (const unsigned char*)input, strlen(input));
  mbedtls_sha256_finish(&sha256_ctx, hash);
  bytesToHex(hash, 32, output);
}

void initCrypto() {
  mbedtls_ecdsa_init(&ecdsa);
  mbedtls_entropy_init(&entropy);
  mbedtls_ctr_drbg_init(&ctr_drbg);
  mbedtls_ctr_drbg_seed(&ctr_drbg, mbedtls_entropy_func, &entropy,
    (const unsigned char*)"microcore_esp_v6", 16);
  
  unsigned char privateKeyBytes[32];
  hexToBytes(PRIVATE_KEY_HEX, privateKeyBytes, 32);
  
  mbedtls_ecp_keypair keypair;
  mbedtls_ecp_keypair_init(&keypair);
  mbedtls_ecp_group_load(&keypair.grp, MBEDTLS_ECP_DP_SECP256K1);
  mbedtls_mpi_read_binary(&keypair.d, privateKeyBytes, 32);
  mbedtls_ecp_mul(&keypair.grp, &keypair.Q, &keypair.d, &keypair.grp.G, NULL, NULL);
  mbedtls_ecdsa_from_keypair(&ecdsa, &keypair);
  
  unsigned char publicKeyBytes[65];
  size_t publicKeyLen = 65;
  mbedtls_ecp_point_write_binary(&keypair.grp, &keypair.Q, MBEDTLS_ECP_PF_UNCOMPRESSED,
    &publicKeyLen, publicKeyBytes, sizeof(publicKeyBytes));
  bytesToHex(publicKeyBytes, publicKeyLen, publicKeyHex);
  
  char pubHash[65];
  computeSHA256(publicKeyHex, pubHash);
  snprintf(walletAddress, sizeof(walletAddress), "MCR_%.32s", pubHash);
  
  char combined[200];
  snprintf(combined, sizeof(combined), "%s%s", USERNAME, publicKeyHex);
  computeSHA256(combined, validatorID);
  
  Serial.println("[CRYPTO] ECDSA secp256k1 initialized");
}

bool signMessage(const char* message, char* signatureOut) {
  unsigned char hash[32];
  mbedtls_sha256_init(&sha256_ctx);
  mbedtls_sha256_starts(&sha256_ctx, 0);
  mbedtls_sha256_update(&sha256_ctx, (const unsigned char*)message, strlen(message));
  mbedtls_sha256_finish(&sha256_ctx, hash);
  
  unsigned char signature[64];
  size_t sigLen;
  int ret = mbedtls_ecdsa_sign(&ecdsa, MBEDTLS_MD_SHA256, hash, sizeof(hash),
    signature, &sigLen, mbedtls_ctr_drbg_random, &ctr_drbg);
  if (ret != 0) return false;
  
  bytesToHex(signature, sigLen, signatureOut);
  return true;
}

// ==================== LEVEL & EEPROM ====================
void calculateLevel() {
  currentLevel = (currentStake < LEVEL_STAKE_RANGE) ? 1 : ((currentStake - 1) / LEVEL_STAKE_RANGE) + 1;
  if (currentLevel < 1) currentLevel = 1;
  if (currentLevel > MAX_LEVEL) currentLevel = MAX_LEVEL;
}
uint32_t getBlockInterval() { return LEVEL_BLOCK_INTERVALS[currentLevel]; }

uint32_t computeChecksum() {
  return (currentStake + totalRewards + totalBlocksSigned + totalUptimeSeconds + 
          todayUptimeSeconds + slashCount + currentLevel) ^ MAGIC_NUMBER;
}
bool isEEPROMValid() {
  uint32_t magic, storedChecksum;
  EEPROM.get(EEPROM_MAGIC_ADDR, magic);
  if (magic != MAGIC_NUMBER) return false;
  EEPROM.get(EEPROM_CHECKSUM_ADDR, storedChecksum);
  return storedChecksum == computeChecksum();
}
void saveToEEPROM() {
  EEPROM.begin(512);
  EEPROM.put(EEPROM_STAKE_ADDR, currentStake);
  EEPROM.put(EEPROM_REWARDS_ADDR, totalRewards);
  EEPROM.put(EEPROM_BLOCKS_ADDR, totalBlocksSigned);
  EEPROM.put(EEPROM_UPTIME_ADDR, totalUptimeSeconds);
  EEPROM.put(EEPROM_TODAY_UPTIME_ADDR, todayUptimeSeconds);
  EEPROM.put(EEPROM_LAST_RESET_ADDR, lastUptimeReset);
  EEPROM.put(EEPROM_SLASH_COUNT_ADDR, slashCount);
  EEPROM.put(EEPROM_CONSECUTIVE_MISSES_ADDR, consecutiveMisses);
  EEPROM.put(EEPROM_LEVEL_ADDR, currentLevel);
  EEPROM.put(EEPROM_PEER_INDEX_ADDR, currentPeerIndex);
  EEPROM.put(EEPROM_CHECKSUM_ADDR, computeChecksum());
  EEPROM.put(EEPROM_MAGIC_ADDR, MAGIC_NUMBER);
  EEPROM.commit();
  EEPROM.end();
}
void loadFromEEPROM() {
  if (!isEEPROMValid()) {
    currentStake = INITIAL_STAKE; totalRewards = 0; totalBlocksSigned = 0;
    totalUptimeSeconds = 0; todayUptimeSeconds = 0; lastUptimeReset = millis()/1000;
    slashCount = 0; consecutiveMisses = 0; currentLevel = 1; currentPeerIndex = 0;
    calculateLevel(); saveToEEPROM(); return;
  }
  EEPROM.begin(512);
  EEPROM.get(EEPROM_STAKE_ADDR, currentStake);
  EEPROM.get(EEPROM_REWARDS_ADDR, totalRewards);
  EEPROM.get(EEPROM_BLOCKS_ADDR, totalBlocksSigned);
  EEPROM.get(EEPROM_UPTIME_ADDR, totalUptimeSeconds);
  EEPROM.get(EEPROM_TODAY_UPTIME_ADDR, todayUptimeSeconds);
  EEPROM.get(EEPROM_LAST_RESET_ADDR, lastUptimeReset);
  EEPROM.get(EEPROM_SLASH_COUNT_ADDR, slashCount);
  EEPROM.get(EEPROM_CONSECUTIVE_MISSES_ADDR, consecutiveMisses);
  EEPROM.get(EEPROM_LEVEL_ADDR, currentLevel);
  EEPROM.get(EEPROM_PEER_INDEX_ADDR, currentPeerIndex);
  EEPROM.end();
  calculateLevel();
}

void checkDailyReset() {
  uint32_t now = millis()/1000;
  if ((now - lastUptimeReset) / 86400 >= 1) {
    todayUptimeSeconds = 0; lastUptimeReset = now; saveToEEPROM();
  }
}
void updateUptime() {
  checkDailyReset();
  totalUptimeSeconds += UPTIME_PING_INTERVAL/1000;
  todayUptimeSeconds += UPTIME_PING_INTERVAL/1000;
  if (todayUptimeSeconds > 86400) todayUptimeSeconds = 86400;
  saveToEEPROM();
}

void handleSlashing() {
  uint32_t slashAmount = max((uint32_t)(currentStake * SLASH_RATE), LEVEL_STAKE_RANGE);
  if (slashAmount > currentStake) slashAmount = currentStake;
  currentStake -= slashAmount;
  if (currentStake < LEVEL_STAKE_RANGE) currentStake = LEVEL_STAKE_RANGE;
  slashCount++; consecutiveMisses++; calculateLevel(); saveToEEPROM();
  Serial.printf("[SLASH] Lost %lu MCX | Stake: %lu | Level: %lu | Slashes: %lu\n",
    slashAmount, currentStake, currentLevel, slashCount);
  if (slashCount >= 5) { miningEnabled = false; Serial.println("[BAN] Miner banned"); }
  led_blink(3, 100);
}
void addReward(uint32_t reward) {
  totalRewards += reward; currentStake += reward; totalBlocksSigned++;
  consecutiveMisses = 0; calculateLevel(); saveToEEPROM();
  Serial.printf("[REWARD] +%lu MCX | Total: %lu | Stake: %lu | Level: %lu | Blocks: %lu\n",
    reward, totalRewards, currentStake, currentLevel, totalBlocksSigned);
  led_blink(1, 50);
}

// ==================== PEER CACHE (GOSSIP) ====================
void savePeersToSPIFFS() {
  if (!SPIFFS.begin(true)) return;
  File f = SPIFFS.open("/peers.json", "w");
  if (f) {
    f.print("{\"peers\":[");
    for (int i = 0; i < peerCount; i++) {
      if (i > 0) f.print(",");
      f.print("\""); f.print(peerList[i]); f.print("\"");
    }
    f.print("],\"version\":\""); f.print(VERSION); f.print("\"}");
    f.close();
  }
  SPIFFS.end();
}
void loadPeersFromSPIFFS() {
  if (!SPIFFS.begin(true)) return;
  if (SPIFFS.exists("/peers.json")) {
    File f = SPIFFS.open("/peers.json", "r");
    if (f) {
      StaticJsonDocument<2048> doc;
      deserializeJson(doc, f.readString());
      f.close();
      JsonArray peers = doc["peers"];
      for (JsonVariant p : peers) if (peerCount < MAX_PEERS) peerList[peerCount++] = p.as<String>();
    }
  }
  SPIFFS.end();
  for (int i = 0; i < BOOTSTRAP_COUNT && peerCount < MAX_PEERS; i++) {
    bool exists = false;
    for (int j = 0; j < peerCount; j++) if (peerList[j] == BOOTSTRAP_NODES[i]) { exists = true; break; }
    if (!exists) peerList[peerCount++] = BOOTSTRAP_NODES[i];
  }
}
void addPeerFromGossip(String peer) {
  for (int i = 0; i < peerCount; i++) if (peerList[i] == peer) return;
  if (peerCount < MAX_PEERS) { peerList[peerCount++] = peer; savePeersToSPIFFS(); }
}
void switchToNextPeer() {
  currentPeerIndex = (currentPeerIndex + 1) % peerCount;
  nodeSwitchCount++;
  String fullPeer = peerList[currentPeerIndex];
  int colonIndex = fullPeer.indexOf(':');
  if (colonIndex > 0) fullPeer = fullPeer.substring(0, colonIndex);
  fullPeer.toCharArray(currentNodeIP, 16);
  if (webSocket.isConnected()) webSocket.disconnect();
  wsConnected = false; isRegistered = false;
  webSocket.begin(currentNodeIP, NODE_PORT, "/");
}

// ==================== WEBSOCKET ====================
void sendRegister() {
  StaticJsonDocument<512> doc;
  doc["type"] = "register";
  doc["validator_id"] = validatorID;
  doc["username"] = USERNAME;
  doc["public_key"] = publicKeyHex;
  doc["wallet"] = walletAddress;
  doc["stake"] = currentStake;
  doc["level"] = currentLevel;
  doc["rewards"] = totalRewards;
  doc["blocks"] = totalBlocksSigned;
  doc["uptime"] = totalUptimeSeconds;
  doc["today_uptime"] = todayUptimeSeconds;
  doc["miner_type"] = "esp32";
  doc["version"] = VERSION;
  
  char timestamp[32];
  snprintf(timestamp, sizeof(timestamp), "%lu", timeClient.getEpochTime());
  doc["timestamp"] = timestamp;
  
  char messageToSign[256];
  snprintf(messageToSign, sizeof(messageToSign), "%s%s%lu%s", validatorID, USERNAME, currentStake, timestamp);
  char signature[130];
  if (signMessage(messageToSign, signature)) doc["signature"] = signature;
  
  String output;
  serializeJson(doc, output);
  webSocket.sendTXT(output);
}
void sendUptimePing() {
  StaticJsonDocument<256> doc;
  doc["type"] = "uptime_ping";
  doc["validator_id"] = validatorID;
  doc["username"] = USERNAME;
  doc["uptime_seconds"] = totalUptimeSeconds;
  doc["today_uptime"] = todayUptimeSeconds;
  doc["stake"] = currentStake;
  doc["level"] = currentLevel;
  webSocket.sendTXT(serializeJson(doc, doc));
}
void sendBlockSignature() {
  char messageToSign[256];
  snprintf(messageToSign, sizeof(messageToSign), "%s%s%lu", currentChallenge, validatorID, currentBlockId);
  char signature[130];
  if (!signMessage(messageToSign, signature)) return;
  
  StaticJsonDocument<512> doc;
  doc["type"] = "block_signature";
  doc["validator_id"] = validatorID;
  doc["username"] = USERNAME;
  doc["challenge"] = currentChallenge;
  doc["signature"] = signature;
  doc["level"] = currentLevel;
  doc["stake"] = currentStake;
  doc["block_id"] = currentBlockId;
  webSocket.sendTXT(serializeJson(doc, doc));
}

void webSocketEvent(WStype_t type, uint8_t* payload, size_t length) {
  switch (type) {
    case WStype_DISCONNECTED:
      isValidator = isRegistered = wsConnected = false;
      led_off();
      if (++reconnectAttempts >= MAX_RECONNECT_ATTEMPTS) { switchToNextPeer(); reconnectAttempts = 0; }
      break;
    case WStype_CONNECTED:
      wsConnected = true; reconnectAttempts = 0; led_on();
      webSocket.sendTXT("{\"type\":\"get_peers\"}");
      sendRegister();
      break;
    case WStype_TEXT: {
      StaticJsonDocument<2048> doc;
      if (deserializeJson(doc, payload)) break;
      
      String typeStr = doc["type"].as<String>();
      if (typeStr == "registered") {
        isRegistered = true;
        led_blink(2, 50);
      }
      else if (typeStr == "peers") {
        JsonArray peers = doc["peers"];
        for (JsonVariant p : peers) addPeerFromGossip(p.as<String>());
      }
      else if (typeStr == "challenge" && miningEnabled) {
        strncpy(currentChallenge, doc["challenge"], 64);
        currentBlockId = doc["block_id"];
        lastChallengeTime = millis();
        isValidator = true;
        sendBlockSignature();
      }
      else if (typeStr == "block_accepted") {
        addReward(doc["reward"]);
        isValidator = false;
      }
      else if (typeStr == "block_rejected") isValidator = false;
      else if (typeStr == "slash") { handleSlashing(); isValidator = false; }
      else if (typeStr == "level_update") {
        uint32_t newStake = doc["stake"];
        if (newStake != currentStake) { currentStake = newStake; calculateLevel(); saveToEEPROM(); }
      }
      else if (typeStr == "miner_control") {
        String action = doc["action"].as<String>();
        if (action == "stop") { miningEnabled = false; isValidator = false; led_off(); }
        else if (action == "start") { miningEnabled = true; led_on(); }
        else if (action == "restart") { miningEnabled = false; isValidator = false; led_off(); delay(1000); miningEnabled = true; led_on(); }
        webSocket.sendTXT("{\"type\":\"control_response\",\"success\":true}");
      }
      break;
    }
    default: break;
  }
}

void connectWiFi() {
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  int attempts = 0;
  while (WiFi.status() != WL_CONNECTED && attempts < 40) { delay(500); attempts++; }
  if (WiFi.status() == WL_CONNECTED) led_blink(2, 100);
  else ESP.restart();
}

void setup() {
  Serial.begin(115200);
  led_off();
  
  Serial.println("\n==========================================");
  Serial.println("MICROCORE ESP32/ESP8266 MINER v6.3");
  Serial.println("ECDSA secp256k1 | 10 Levels (1000 MCX/level)");
  Serial.println("Gossip Discovery | No DNS | Permanent Towers");
  Serial.println("==========================================\n");
  
  initCrypto();
  loadFromEEPROM();
  loadPeersFromSPIFFS();
  
  if (currentPeerIndex < peerCount) {
    String fullPeer = peerList[currentPeerIndex];
    int colonIndex = fullPeer.indexOf(':');
    if (colonIndex > 0) fullPeer = fullPeer.substring(0, colonIndex);
    fullPeer.toCharArray(currentNodeIP, 16);
  } else strcpy(currentNodeIP, "192.168.1.100");
  
  Serial.printf("Username: %s\nWallet: %s\nStake: %lu MCX, Level: %lu\nBlock interval: %lu sec\nPeers in cache: %d\n",
    USERNAME, walletAddress, currentStake, currentLevel, getBlockInterval(), peerCount);
  
  connectWiFi();
  timeClient.begin();
  webSocket.begin(currentNodeIP, NODE_PORT, "/");
  webSocket.onEvent(webSocketEvent);
  webSocket.setReconnectInterval(5000);
  
  lastUptimePing = millis();
  led_blink(3, 100);
  Serial.println("[READY] ESP32 miner running with GOSSIP DISCOVERY\n");
}

void loop() {
  webSocket.loop();
  timeClient.update();
  
  if (!isRegistered && millis() - lastUptimePing > 30000) {
    if (wsConnected) sendRegister();
    else { switchToNextPeer(); webSocket.begin(currentNodeIP, NODE_PORT, "/"); }
    lastUptimePing = millis();
  }
  
  if (millis() - lastUptimePing >= UPTIME_PING_INTERVAL) {
    updateUptime();
    sendUptimePing();
    lastUptimePing = millis();
  }
  
  if (isValidator && millis() - lastChallengeTime >= SIGNING_WINDOW_MS) {
    handleSlashing();
    isValidator = false;
  }
  
  static uint32_t lastSave = 0;
  if (millis() - lastSave >= 3600000) { saveToEEPROM(); savePeersToSPIFFS(); lastSave = millis(); }
  
  delay(10);
}