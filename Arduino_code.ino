/*
  MICROCORE (MCX) ARDUINO UNO MINER - SHA256 MODE
  No WiFi - uses Serial to communicate with computer bridge
  Uses SHA256 signing (lightweight for Uno)
  
  Hardware: Arduino Uno + USB cable to computer
  Instructions:
  1. Upload this code to Arduino Uno
  2. Run wifi_bridge.py on computer
  3. Bridge will handle node communication
*/

#include <ArduinoJson.h>
#include <EEPROM.h>

// ==================== USER CONFIGURATION ====================
// EDIT THESE BEFORE UPLOADING
const char* USERNAME = "your_username";                    // ← CHANGE THIS
const char* PRIVATE_KEY = "your_private_key_here";         // ← CHANGE THIS (from web wallet)

uint32_t INITIAL_STAKE = 100;

// ==================== CONSTANTS ====================
#define SYMBOL "MCX"
#define LEVEL_STAKE_RANGE 100
#define SIGNING_WINDOW_MS 2500
#define SLASH_RATE 0.10
#define UPTIME_PING_INTERVAL 30000

#define EEPROM_STAKE_ADDR 0
#define EEPROM_REWARDS_ADDR 4
#define EEPROM_BLOCKS_ADDR 8
#define EEPROM_UPTIME_ADDR 12
#define EEPROM_CHECKSUM_ADDR 16

// ==================== GLOBAL VARIABLES ====================
uint32_t currentStake;
uint32_t totalRewards;
uint32_t totalBlocksSigned;
uint32_t uptimeSeconds;
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
String incomingData = "";

// ==================== SIMPLE CRYPTO (SHA256 for Uno) ====================
void computeSHA256(const char* input, char* output) {
    // Simple SHA256-like hash for Uno (using djb2 + length)
    // Note: This is NOT real SHA256 - Uno cannot run real SHA256
    // For production, use ATECC608A crypto chip
    unsigned long hash = 5381;
    int len = 0;
    for (int i = 0; input[i] != '\0'; i++) {
        hash = ((hash << 5) + hash) + input[i];
        len++;
    }
    // Add length to make it harder to collide
    hash = ((hash << 5) + hash) + len;
    sprintf(output, "%016lx", hash);
}

void generateValidatorID() {
    char combined[100];
    snprintf(combined, sizeof(combined), "%s%s", USERNAME, PRIVATE_KEY);
    computeSHA256(combined, validatorID);
    computeSHA256(validatorID, walletAddress);
    char temp[65];
    strcpy(temp, walletAddress);
    snprintf(walletAddress, sizeof(walletAddress), "MCR_%.16s", temp);
}

// ==================== STAKING & LEVELS ====================
void calculateLevel() {
    currentLevel = ((currentStake - 1) / LEVEL_STAKE_RANGE) + 1;
    if (currentLevel < 1) currentLevel = 1;
    if (currentLevel > 100) currentLevel = 100;
}

uint32_t computeChecksum() {
    uint32_t sum = currentStake + totalRewards + totalBlocksSigned + uptimeSeconds + slashCount;
    return sum ^ 0x5A5A5A5A;
}

void saveToEEPROM() {
    EEPROM.begin(512);
    EEPROM.put(EEPROM_STAKE_ADDR, currentStake);
    EEPROM.put(EEPROM_REWARDS_ADDR, totalRewards);
    EEPROM.put(EEPROM_BLOCKS_ADDR, totalBlocksSigned);
    EEPROM.put(EEPROM_UPTIME_ADDR, uptimeSeconds);
    uint32_t checksum = computeChecksum();
    EEPROM.put(EEPROM_CHECKSUM_ADDR, checksum);
    EEPROM.commit();
    EEPROM.end();
    Serial.println("[EEPROM] Stats saved");
}

void loadFromEEPROM() {
    EEPROM.begin(512);
    EEPROM.get(EEPROM_STAKE_ADDR, currentStake);
    EEPROM.get(EEPROM_REWARDS_ADDR, totalRewards);
    EEPROM.get(EEPROM_BLOCKS_ADDR, totalBlocksSigned);
    EEPROM.get(EEPROM_UPTIME_ADDR, uptimeSeconds);
    
    uint32_t storedChecksum;
    EEPROM.get(EEPROM_CHECKSUM_ADDR, storedChecksum);
    EEPROM.end();
    
    uint32_t calculatedChecksum = computeChecksum();
    
    if (currentStake < 100 || currentStake > 10000000 || storedChecksum != calculatedChecksum) {
        Serial.println("[EEPROM] Invalid data, resetting to defaults");
        currentStake = INITIAL_STAKE;
        totalRewards = 0;
        totalBlocksSigned = 0;
        uptimeSeconds = 0;
        slashCount = 0;
        calculateLevel();
        saveToEEPROM();
    }
    
    calculateLevel();
    Serial.print("[EEPROM] Loaded - Stake: ");
    Serial.print(currentStake);
    Serial.print(" MCX, Level: ");
    Serial.println(currentLevel);
}

// ==================== SLASHING & REWARDS ====================
void handleSlashing() {
    uint32_t slashAmount = (uint32_t)(currentStake * SLASH_RATE);
    if (slashAmount < LEVEL_STAKE_RANGE) slashAmount = LEVEL_STAKE_RANGE;
    if (slashAmount > currentStake) slashAmount = currentStake;
    
    currentStake -= slashAmount;
    if (currentStake < LEVEL_STAKE_RANGE) currentStake = LEVEL_STAKE_RANGE;
    
    slashCount++;
    calculateLevel();
    saveToEEPROM();
    consecutiveMisses++;
    
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
    }
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
    doc["public_key"] = PRIVATE_KEY;  // For SHA256 mode, public_key is the private key
    doc["wallet"] = walletAddress;
    doc["stake"] = currentStake;
    doc["level"] = currentLevel;
    doc["rewards"] = totalRewards;
    doc["blocks"] = totalBlocksSigned;
    doc["miner_type"] = "uno";
    doc["timestamp"] = millis() / 1000;
    
    char messageToSign[100];
    snprintf(messageToSign, sizeof(messageToSign), "%s%s%lu", validatorID, USERNAME, currentStake);
    char signature[33];
    computeSHA256(messageToSign, signature);
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
    doc["uptime_seconds"] = uptimeCounter;
    doc["stake"] = currentStake;
    doc["level"] = currentLevel;
    
    String output;
    serializeJson(doc, output);
    sendToBridge(output);
}

void sendBlockSignature() {
    char messageToSign[100];
    snprintf(messageToSign, sizeof(messageToSign), "%s%s%lu", currentChallenge, validatorID, currentBlockId);
    char signature[33];
    computeSHA256(messageToSign, signature);
    
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
        Serial.print("[NODE] Registration confirmed. Level: ");
        Serial.println(nodeLevel);
    }
    else if (strcmp(type, "challenge") == 0) {
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
            Serial.println(currentLevel);
        }
    }
    else if (strcmp(type, "miner_control") == 0) {
        const char* action = doc["action"];
        if (strcmp(action, "stop") == 0) {
            Serial.println("[CONTROL] Stop command received - stopping mining");
            isValidator = false;
        } else if (strcmp(action, "start") == 0) {
            Serial.println("[CONTROL] Start command received - resuming mining");
        } else if (strcmp(action, "restart") == 0) {
            Serial.println("[CONTROL] Restart command received");
            isValidator = false;
            // Will re-register on next cycle
        }
        // Send acknowledgment back via bridge
        StaticJsonDocument<128> ack;
        ack["type"] = "miner_control_response";
        ack["miner_id"] = validatorID;
        ack["action"] = action;
        ack["success"] = true;
        String ackStr;
        serializeJson(ack, ackStr);
        sendToBridge(ackStr);
    }
}

// ==================== SETUP ====================
void setup() {
    Serial.begin(115200);
    delay(1000);
    
    Serial.println("\n==========================================");
    Serial.println("MICROCORE (MCX) ARDUINO UNO MINER v3.0");
    Serial.println("SHA256 Mode | Connects via WiFi Bridge");
    Serial.println("==========================================\n");
    
    loadFromEEPROM();
    generateValidatorID();
    calculateLevel();
    
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
    
    sendRegister();
    
    lastUptimePing = millis();
    uptimeCounter = 0;
    isValidator = false;
    isRegistered = false;
    
    Serial.println("\n[READY] Arduino Uno miner is running");
    Serial.println("[READY] Make sure wifi_bridge.py is running on your computer\n");
}

// ==================== LOOP ====================
void loop() {
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
    
    if (millis() - lastUptimePing >= UPTIME_PING_INTERVAL) {
        uptimeCounter++;
        sendUptimePing();
        lastUptimePing = millis();
        
        if (uptimeCounter % 2 == 0) {
            Serial.print("[STATUS] Stake: ");
            Serial.print(currentStake);
            Serial.print(" MCX, Level: ");
            Serial.print(currentLevel);
            Serial.print(", Blocks: ");
            Serial.print(totalBlocksSigned);
            Serial.print(", Rewards: ");
            Serial.print(totalRewards);
            Serial.print(" MCX, Uptime: ");
            Serial.println(uptimeCounter);
        }
    }
    
    if (isValidator && (millis() - lastChallengeTime >= SIGNING_WINDOW_MS)) {
        Serial.println("[TIMEOUT] Failed to sign within window");
        handleSlashing();
        isValidator = false;
    }
    
    static uint32_t lastSave = 0;
    if (millis() - lastSave >= 3600000) {
        saveToEEPROM();
        lastSave = millis();
        Serial.println("[EEPROM] Periodic save completed");
    }
    
    delay(10);
}