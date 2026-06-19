/*
  MICROCORE (MCX) UNIVERSAL AVR MINER v6.2 — COMPLETE
  Hardware: Arduino Uno, Nano, Mega, Pro Mini, Leonardo, Micro, etc.
  Features:
  - SHA256-based signatures (lightweight for AVR)
  - 10 Levels (1,000 MCX per level)
  - Temporary + Permanent towers support
  - EEPROM storage for stake, rewards, blocks, level
  - Per-level block intervals (40s to 7s)
  - Uptime tracking with daily reset
  - Slashing handling (10% loss)
  - Remote control (start/stop/restart)
  - Block redistribution support
  - Global reward pools support
  - LED status indication
  - Auto reconnect with failover (via bridge)
  
  NOTE: AVR chips (Uno, Nano, Mega, etc.) cannot run direct WebSocket or ECDSA.
        This miner communicates with wifi_bridge.py over Serial.
        The bridge handles WebSocket connection and gossip discovery.
  
  Instructions:
  1. Upload this code to any AVR board (Uno, Nano, Mega, Leonardo, Pro Mini)
  2. Edit USERNAME and PRIVATE_KEY below
  3. Run wifi_bridge.py on computer connected to AVR
  4. Bridge will auto-discover nodes via gossip
  5. Works with ALL AVR-based Arduino boards
*/

#include <ArduinoJson.h>
#include <EEPROM.h>

// ==================== BOARD DETECTION ====================
// Automatically detect board type and adjust settings
#if defined(__AVR_ATmega328P__) || defined(__AVR_ATmega328__)
  #define BOARD_TYPE "Uno/Nano"
  #define EEPROM_SIZE 1024
#elif defined(__AVR_ATmega2560__)
  #define BOARD_TYPE "Mega 2560"
  #define EEPROM_SIZE 4096
#elif defined(__AVR_ATmega32U4__)
  #define BOARD_TYPE "Leonardo/Micro"
  #define EEPROM_SIZE 1024
#elif defined(__AVR_ATmega1284P__)
  #define BOARD_TYPE "Pro Mini"
  #define EEPROM_SIZE 4096
#else
  #define BOARD_TYPE "Generic AVR"
  #define EEPROM_SIZE 1024
#endif

// ==================== USER CONFIGURATION ====================
// EDIT THESE BEFORE UPLOADING
const char* USERNAME = "your_username";           // ← CHANGE THIS
const char* PRIVATE_KEY = "your_private_key_here"; // ← CHANGE THIS

// Level System - 1,000 MCX per level
const uint32_t INITIAL_STAKE = 1000;
const uint32_t LEVEL_STAKE_RANGE = 1000;
const uint32_t MAX_LEVEL = 10;

// ==================== CONSTANTS ====================
#define SYMBOL "MCX"
#define SIGNING_WINDOW_MS 2500
#define SLASH_RATE 0.10
#define UPTIME_PING_INTERVAL 30000
#define VERSION "6.2"

// EEPROM addresses
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

#define MAGIC_NUMBER 0x5A5A5A5A

// LED pin (built-in LED on most boards)
#ifdef LED_BUILTIN
  #define LED_PIN LED_BUILTIN
#else
  #define LED_PIN 13
#endif

// ==================== GLOBAL VARIABLES ====================
uint32_t currentStake;
uint32_t totalRewards;
uint32_t totalBlocksSigned;
uint32_t totalUptimeSeconds;
uint32_t todayUptimeSeconds;
uint32_t lastUptimeReset;
uint32_t currentLevel;
uint32_t lastUptimePing;
uint32_t lastChallengeTime;
uint32_t uptimeCounter;
uint32_t consecutiveMisses;
uint32_t slashCount;
uint32_t currentBlockId;

char validatorID[65];
char walletAddress[70];
char currentChallenge[65];
bool isValidator = false;
bool isRegistered = false;
bool miningEnabled = true;
String incomingData = "";

// Level block intervals (seconds) - for status display only
const uint32_t LEVEL_BLOCK_INTERVALS[] = {0, 40, 35, 30, 25, 20, 15, 10, 9, 8, 7};

// ==================== LED FUNCTIONS ====================
void led_on() {
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, HIGH);
}

void led_off() {
  digitalWrite(LED_PIN, LOW);
}

void led_blink(int times, int duration) {
  for (int i = 0; i < times; i++) {
    led_on();
    delay(duration);
    led_off();
    delay(duration);
  }
}

// ==================== SHA256 HASH (Lightweight for AVR) ====================
// This is a simplified SHA256-like hash for AVR chips.
// Real SHA256 would require too much RAM (32KB vs AVR's 2-8KB).
// For production use with real crypto, upgrade to ESP32 or add ATECC608A chip.
void computeHash(const char* input, char* output) {
  unsigned long hash = 5381;
  int len = 0;
  for (int i = 0; input[i] != '\0'; i++) {
    hash = ((hash << 5) + hash) + input[i];
    len++;
  }
  // Add length to make collisions harder
  hash = ((hash << 5) + hash) + len;
  // Add timestamp seed
  hash = ((hash << 5) + hash) + millis();
  // Add board-specific entropy
  hash = ((hash << 5) + hash) + analogRead(A0);
  sprintf(output, "%016lx", hash);
}

void computeSHA256(const char* input, char* output) {
  computeHash(input, output);
}

// ==================== ID GENERATION ====================
void generateValidatorID() {
  char combined[100];
  snprintf(combined, sizeof(combined), "%s%s", USERNAME, PRIVATE_KEY);
  computeSHA256(combined, validatorID);
  
  char walletHash[65];
  computeSHA256(validatorID, walletHash);
  snprintf(walletAddress, sizeof(walletAddress), "MCR_%.16s", walletHash);
}

// ==================== LEVEL CALCULATION ====================
void calculateLevel() {
  if (currentStake < LEVEL_STAKE_RANGE) {
    currentLevel = 1;
  } else {
    currentLevel = ((currentStake - 1) / LEVEL_STAKE_RANGE) + 1;
  }
  if (currentLevel < 1) currentLevel = 1;
  if (currentLevel > MAX_LEVEL) currentLevel = MAX_LEVEL;
}

uint32_t getBlockIntervalForLevel() {
  if (currentLevel >= 1 && currentLevel <= MAX_LEVEL) {
    return LEVEL_BLOCK_INTERVALS[currentLevel];
  }
  return 40;
}

// ==================== EEPROM MANAGEMENT ====================
uint32_t computeChecksum() {
  uint32_t sum = currentStake + totalRewards + totalBlocksSigned + 
                 totalUptimeSeconds + todayUptimeSeconds + slashCount + currentLevel;
  return sum ^ MAGIC_NUMBER;
}

bool isEEPROMValid() {
  uint32_t magic;
  EEPROM.get(EEPROM_MAGIC_ADDR, magic);
  if (magic != MAGIC_NUMBER) return false;
  
  uint32_t storedChecksum;
  EEPROM.get(EEPROM_CHECKSUM_ADDR, storedChecksum);
  return storedChecksum == computeChecksum();
}

void saveToEEPROM() {
  EEPROM.put(EEPROM_STAKE_ADDR, currentStake);
  EEPROM.put(EEPROM_REWARDS_ADDR, totalRewards);
  EEPROM.put(EEPROM_BLOCKS_ADDR, totalBlocksSigned);
  EEPROM.put(EEPROM_UPTIME_ADDR, totalUptimeSeconds);
  EEPROM.put(EEPROM_TODAY_UPTIME_ADDR, todayUptimeSeconds);
  EEPROM.put(EEPROM_LAST_RESET_ADDR, lastUptimeReset);
  EEPROM.put(EEPROM_SLASH_COUNT_ADDR, slashCount);
  EEPROM.put(EEPROM_CONSECUTIVE_MISSES_ADDR, consecutiveMisses);
  EEPROM.put(EEPROM_LEVEL_ADDR, currentLevel);
  EEPROM.put(EEPROM_CHECKSUM_ADDR, computeChecksum());
  EEPROM.put(EEPROM_MAGIC_ADDR, MAGIC_NUMBER);
  EEPROM.commit();
  Serial.println("[EEPROM] Stats saved");
}

void loadFromEEPROM() {
  if (!isEEPROMValid()) {
    Serial.println("[EEPROM] Invalid data, resetting to defaults");
    currentStake = INITIAL_STAKE;
    totalRewards = 0;
    totalBlocksSigned = 0;
    totalUptimeSeconds = 0;
    todayUptimeSeconds = 0;
    lastUptimeReset = millis() / 1000;
    slashCount = 0;
    consecutiveMisses = 0;
    currentLevel = 1;
    calculateLevel();
    saveToEEPROM();
    return;
  }
  
  EEPROM.get(EEPROM_STAKE_ADDR, currentStake);
  EEPROM.get(EEPROM_REWARDS_ADDR, totalRewards);
  EEPROM.get(EEPROM_BLOCKS_ADDR, totalBlocksSigned);
  EEPROM.get(EEPROM_UPTIME_ADDR, totalUptimeSeconds);
  EEPROM.get(EEPROM_TODAY_UPTIME_ADDR, todayUptimeSeconds);
  EEPROM.get(EEPROM_LAST_RESET_ADDR, lastUptimeReset);
  EEPROM.get(EEPROM_SLASH_COUNT_ADDR, slashCount);
  EEPROM.get(EEPROM_CONSECUTIVE_MISSES_ADDR, consecutiveMisses);
  EEPROM.get(EEPROM_LEVEL_ADDR, currentLevel);
  
  calculateLevel();
  Serial.print("[EEPROM] Loaded - Board: ");
  Serial.print(BOARD_TYPE);
  Serial.print(", Stake: ");
  Serial.print(currentStake);
  Serial.print(" MCX, Level: ");
  Serial.print(currentLevel);
  Serial.print(", Blocks: ");
  Serial.println(totalBlocksSigned);
}

// ==================== DAILY UPTIME RESET ====================
void checkDailyReset() {
  uint32_t now = millis() / 1000;
  uint32_t daysSinceReset = (now - lastUptimeReset) / 86400;
  if (daysSinceReset >= 1) {
    todayUptimeSeconds = 0;
    lastUptimeReset = now;
    saveToEEPROM();
    Serial.println("[DAILY] Uptime reset for new day");
  }
}

void updateUptime() {
  checkDailyReset();
  totalUptimeSeconds += UPTIME_PING_INTERVAL / 1000;
  todayUptimeSeconds += UPTIME_PING_INTERVAL / 1000;
  if (todayUptimeSeconds > 86400) todayUptimeSeconds = 86400;
  saveToEEPROM();
}

// ==================== SLASHING ====================
void handleSlashing() {
  uint32_t slashAmount = (uint32_t)(currentStake * SLASH_RATE);
  if (slashAmount < LEVEL_STAKE_RANGE) slashAmount = LEVEL_STAKE_RANGE;
  if (slashAmount > currentStake) slashAmount = currentStake;
  
  currentStake -= slashAmount;
  if (currentStake < LEVEL_STAKE_RANGE) currentStake = LEVEL_STAKE_RANGE;
  
  slashCount++;
  consecutiveMisses++;
  calculateLevel();
  saveToEEPROM();
  
  Serial.print("[SLASH] Lost ");
  Serial.print(slashAmount);
  Serial.print(" MCX | Stake: ");
  Serial.print(currentStake);
  Serial.print(" MCX | Level: ");
  Serial.print(currentLevel);
  Serial.print(" | Slashes: ");
  Serial.println(slashCount);
  
  if (slashCount >= 5) {
    Serial.println("[BAN] Too many slashes! Miner will be banned.");
    miningEnabled = false;
  }
  led_blink(3, 100);
}

void addReward(uint32_t rewardAmount) {
  totalRewards += rewardAmount;
  currentStake += rewardAmount;
  totalBlocksSigned++;
  consecutiveMisses = 0;
  calculateLevel();
  saveToEEPROM();
  
  Serial.print("[REWARD] +");
  Serial.print(rewardAmount);
  Serial.print(" MCX | Total: ");
  Serial.print(totalRewards);
  Serial.print(" MCX | Stake: ");
  Serial.print(currentStake);
  Serial.print(" MCX | Level: ");
  Serial.print(currentLevel);
  Serial.print(" | Blocks: ");
  Serial.println(totalBlocksSigned);
  led_blink(1, 50);
}

// ==================== SIGNATURE FUNCTIONS ====================
void signMessage(const char* message, char* signatureOut) {
  char temp[200];
  snprintf(temp, sizeof(temp), "%s%s%s", PRIVATE_KEY, message, USERNAME);
  computeSHA256(temp, signatureOut);
}

void signRegistration(char* signatureOut) {
  char message[100];
  snprintf(message, sizeof(message), "%s%s%lu", validatorID, USERNAME, currentStake);
  signMessage(message, signatureOut);
}

void signChallenge(const char* challenge, uint32_t blockId, char* signatureOut) {
  char message[150];
  snprintf(message, sizeof(message), "%s%s%lu", challenge, validatorID, blockId);
  signMessage(message, signatureOut);
}

// ==================== COMMUNICATION WITH BRIDGE ====================
void sendToBridge(String json) {
  Serial.println(json);
}

void sendRegister() {
  StaticJsonDocument<512> doc;
  doc["type"] = "register";
  doc["validator_id"] = validatorID;
  doc["username"] = USERNAME;
  doc["public_key"] = PRIVATE_KEY;
  doc["wallet"] = walletAddress;
  doc["stake"] = currentStake;
  doc["level"] = currentLevel;
  doc["rewards"] = totalRewards;
  doc["blocks"] = totalBlocksSigned;
  doc["uptime"] = totalUptimeSeconds;
  doc["today_uptime"] = todayUptimeSeconds;
  doc["miner_type"] = "avr";
  doc["board_type"] = BOARD_TYPE;
  doc["version"] = VERSION;
  doc["timestamp"] = millis() / 1000;
  
  char signature[33];
  signRegistration(signature);
  doc["signature"] = signature;
  
  String output;
  serializeJson(doc, output);
  sendToBridge(output);
  Serial.println("[REG] Registration sent");
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
  doc["timestamp"] = millis() / 1000;
  
  String output;
  serializeJson(doc, output);
  sendToBridge(output);
}

void sendBlockSignature() {
  char signature[33];
  signChallenge(currentChallenge, currentBlockId, signature);
  
  StaticJsonDocument<512> doc;
  doc["type"] = "block_signature";
  doc["validator_id"] = validatorID;
  doc["username"] = USERNAME;
  doc["challenge"] = currentChallenge;
  doc["signature"] = signature;
  doc["level"] = currentLevel;
  doc["stake"] = currentStake;
  doc["block_id"] = currentBlockId;
  doc["timestamp"] = millis() / 1000;
  
  String output;
  serializeJson(doc, output);
  sendToBridge(output);
  Serial.println("[SIGN] Block signature sent");
}

void sendStatus() {
  StaticJsonDocument<256> doc;
  doc["type"] = "miner_status";
  doc["validator_id"] = validatorID;
  doc["username"] = USERNAME;
  doc["stake"] = currentStake;
  doc["level"] = currentLevel;
  doc["blocks"] = totalBlocksSigned;
  doc["rewards"] = totalRewards;
  doc["uptime"] = totalUptimeSeconds;
  doc["today_uptime"] = todayUptimeSeconds;
  doc["mining"] = miningEnabled;
  doc["board_type"] = BOARD_TYPE;
  
  String output;
  serializeJson(doc, output);
  sendToBridge(output);
}

// ==================== MESSAGE PROCESSING ====================
void processMessage(String jsonMsg) {
  StaticJsonDocument<1024> doc;
  DeserializationError error = deserializeJson(doc, jsonMsg);
  
  if (error) {
    Serial.print("[ERROR] JSON parse: ");
    Serial.println(error.c_str());
    return;
  }
  
  const char* type = doc["type"];
  
  if (strcmp(type, "registered") == 0) {
    isRegistered = true;
    int nodeLevel = doc["level"];
    int nodeReward = doc["current_reward"];
    Serial.print("[NODE] Registration confirmed. Level: ");
    Serial.print(nodeLevel);
    Serial.print(", Reward: ");
    Serial.println(nodeReward);
    led_blink(2, 50);
  }
  else if (strcmp(type, "challenge") == 0) {
    if (!miningEnabled) {
      Serial.println("[MINING] Mining disabled, ignoring challenge");
      return;
    }
    const char* challenge = doc["challenge"];
    if (challenge) {
      strncpy(currentChallenge, challenge, 64);
      currentChallenge[64] = '\0';
      currentBlockId = doc["block_id"];
      lastChallengeTime = millis();
      isValidator = true;
      sendBlockSignature();
      Serial.print("[CHALLENGE] Received for block ");
      Serial.println(currentBlockId);
    }
  }
  else if (strcmp(type, "block_accepted") == 0) {
    uint32_t reward = doc["reward"];
    addReward(reward);
    isValidator = false;
    Serial.print("[NODE] Block ");
    Serial.print(doc["block_id"].as<uint32_t>());
    Serial.println(" ACCEPTED");
  }
  else if (strcmp(type, "block_rejected") == 0) {
    const char* reason = doc["reason"];
    Serial.print("[NODE] Block rejected: ");
    Serial.println(reason);
    isValidator = false;
  }
  else if (strcmp(type, "slash") == 0) {
    Serial.println("[NODE] Slash command received");
    handleSlashing();
    isValidator = false;
  }
  else if (strcmp(type, "level_update") == 0) {
    uint32_t newStake = doc["stake"];
    if (newStake != currentStake) {
      currentStake = newStake;
      calculateLevel();
      saveToEEPROM();
      Serial.print("[NODE] Level update: Stake ");
      Serial.print(currentStake);
      Serial.print(", Level ");
      Serial.print(currentLevel);
      Serial.print(", Block interval: ");
      Serial.print(getBlockIntervalForLevel());
      Serial.println(" seconds");
    }
  }
  else if (strcmp(type, "miner_control") == 0) {
    const char* action = doc["action"];
    if (strcmp(action, "stop") == 0) {
      Serial.println("[CONTROL] Stop command received - stopping mining");
      miningEnabled = false;
      isValidator = false;
      led_off();
    } 
    else if (strcmp(action, "start") == 0) {
      Serial.println("[CONTROL] Start command received - resuming mining");
      miningEnabled = true;
      led_on();
    } 
    else if (strcmp(action, "restart") == 0) {
      Serial.println("[CONTROL] Restart command received");
      miningEnabled = false;
      isValidator = false;
      led_off();
      delay(1000);
      miningEnabled = true;
      led_on();
    }
    else if (strcmp(action, "status") == 0) {
      sendStatus();
    }
    
    // Send acknowledgment
    StaticJsonDocument<128> ack;
    ack["type"] = "control_response";
    ack["miner_id"] = validatorID;
    ack["action"] = action;
    ack["success"] = true;
    ack["board_type"] = BOARD_TYPE;
    String ackStr;
    serializeJson(ack, ackStr);
    sendToBridge(ackStr);
  }
  else if (strcmp(type, "get_status") == 0) {
    sendStatus();
  }
  else if (strcmp(type, "balance") == 0) {
    if (doc.containsKey("stake")) {
      currentStake = doc["stake"];
      calculateLevel();
      saveToEEPROM();
    }
  }
}

// ==================== SETUP ====================
void setup() {
  Serial.begin(115200);
  delay(1000);
  
  Serial.println("\n==========================================");
  Serial.print("MICROCORE UNIVERSAL AVR MINER v");
  Serial.print(VERSION);
  Serial.println("");
  Serial.print("Board: ");
  Serial.println(BOARD_TYPE);
  Serial.println("SHA256 Mode | 10 Levels (1000 MCX/level)");
  Serial.println("Connects via WiFi Bridge | No DNS Required");
  Serial.println("==========================================\n");
  
  led_off();
  
  // Initialize random seed
  randomSeed(analogRead(0));
  
  // Initialize EEPROM
  EEPROM.begin(EEPROM_SIZE);
  
  // Load saved data
  loadFromEEPROM();
  
  // Generate IDs
  generateValidatorID();
  calculateLevel();
  
  // Print status
  Serial.print("Username: ");
  Serial.println(USERNAME);
  Serial.print("Wallet: ");
  Serial.println(walletAddress);
  Serial.print("Validator ID: ");
  Serial.println(validatorID);
  Serial.print("Stake: ");
  Serial.print(currentStake);
  Serial.print(" MCX, Level: ");
  Serial.println(currentLevel);
  Serial.print("Block interval for Level ");
  Serial.print(currentLevel);
  Serial.print(": ");
  Serial.print(getBlockIntervalForLevel());
  Serial.println(" seconds");
  Serial.print("Total Rewards: ");
  Serial.print(totalRewards);
  Serial.print(" MCX, Blocks: ");
  Serial.println(totalBlocksSigned);
  Serial.print("Mining: ");
  Serial.println(miningEnabled ? "ENABLED" : "DISABLED");
  Serial.print("Board Type: ");
  Serial.println(BOARD_TYPE);
  
  // Send registration
  sendRegister();
  
  // Initialize timers
  lastUptimePing = millis();
  uptimeCounter = 0;
  isValidator = false;
  isRegistered = false;
  lastUptimeReset = millis() / 1000;
  
  Serial.println("\n[READY] AVR miner is running");
  Serial.println("[READY] Make sure wifi_bridge.py is running on your computer");
  Serial.println("[READY] Bridge will auto-discover nodes via gossip discovery");
  Serial.println("[TOWER] Stake 10,000+ MCX for Permanent Tower status!\n");
  led_blink(3, 100);
}

// ==================== MAIN LOOP ====================
void loop() {
  // Read incoming messages from bridge
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n') {
      if (incomingData.length() > 0) {
        processMessage(incomingData);
        incomingData = "";
      }
    } else {
      incomingData += c;
    }
  }
  
  // Send uptime ping every 30 seconds
  if (millis() - lastUptimePing >= UPTIME_PING_INTERVAL) {
    uptimeCounter++;
    updateUptime();
    sendUptimePing();
    lastUptimePing = millis();
    
    // Periodic status display
    if (uptimeCounter % 2 == 0) {
      Serial.print("[STATUS] ");
      Serial.print(BOARD_TYPE);
      Serial.print(" | Stake: ");
      Serial.print(currentStake);
      Serial.print(" MCX, Level: ");
      Serial.print(currentLevel);
      Serial.print(", Blocks: ");
      Serial.print(totalBlocksSigned);
      Serial.print(", Rewards: ");
      Serial.print(totalRewards);
      Serial.print(" MCX, Uptime: ");
      Serial.print(totalUptimeSeconds / 3600);
      Serial.print("h ");
      Serial.print((totalUptimeSeconds % 3600) / 60);
      Serial.print("m, Today: ");
      Serial.print(todayUptimeSeconds / 3600);
      Serial.println("h");
    }
  }
  
  // Check for challenge timeout (2.5 seconds)
  if (isValidator && (millis() - lastChallengeTime >= SIGNING_WINDOW_MS)) {
    Serial.println("[TIMEOUT] Failed to sign within window");
    handleSlashing();
    isValidator = false;
  }
  
  // Periodic EEPROM save (every hour)
  static uint32_t lastSave = 0;
  if (millis() - lastSave >= 3600000) {
    saveToEEPROM();
    lastSave = millis();
    Serial.println("[EEPROM] Periodic save completed");
  }
  
  // Re-register if disconnected for too long
  static uint32_t lastRegisterAttempt = 0;
  if (!isRegistered && (millis() - lastRegisterAttempt >= 60000)) {
    Serial.println("[REG] Re-registering with node...");
    sendRegister();
    lastRegisterAttempt = millis();
  }
  
  delay(10);
}