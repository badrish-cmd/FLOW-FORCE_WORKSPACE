/**
 * TableWebSocketClient
 * Manages WebSocket connection to /ws/tables/<table_id>/ for real-time table and task synchronization.
 * 
 * Features:
 * - Dynamic URL construction using current location (ws:// or wss://)
 * - Safe authentication using browser session cookies
 * - Duplicate connection prevention
 * - Bounded exponential reconnect backoff with jitter
 * - Graceful lifecycle management (connect, reconnect, close, destroy)
 * - Event-driven architecture with targeted handlers
 * - Fallback resilience: fails open without breaking HTTP operations
 */

class TableWebSocketClient {
    constructor(options = {}) {
        this.tableId = options.tableId;
        this.onEvent = options.onEvent || (() => {});
        this.onStatusChange = options.onStatusChange || (() => {});
        
        // Configuration
        this.baseReconnectDelay = options.baseReconnectDelay || 1000;
        this.maxReconnectDelay = options.maxReconnectDelay || 30000;
        this.backoffMultiplier = 1.5;
        
        // State
        this.socket = null;
        this.status = 'offline'; // 'offline' | 'connecting' | 'connected' | 'reconnecting'
        this.reconnectAttempts = 0;
        this.reconnectTimer = null;
        this.isDestroyed = false;
        this.shouldReconnect = true;
    }

    getWebSocketUrl() {
        if (!this.tableId) {
            throw new Error("TableWebSocketClient requires a valid tableId");
        }
        const protocol = (typeof window !== 'undefined' && window.location && window.location.protocol === 'https:') ? 'wss:' : 'ws:';
        const host = (typeof window !== 'undefined' && window.location && window.location.host) ? window.location.host : 'localhost:8000';
        return `${protocol}//${host}/ws/tables/${this.tableId}/`;
    }

    setStatus(newStatus) {
        if (this.status !== newStatus) {
            this.status = newStatus;
            try {
                this.onStatusChange(newStatus);
            } catch (err) {
                console.error("[TableWebSocketClient] Error in onStatusChange callback:", err);
            }
        }
    }

    connect() {
        if (this.isDestroyed) return;
        
        // Prevent duplicate connections if already open or connecting
        if (this.socket && (this.socket.readyState === 0 || this.socket.readyState === 1)) {
            return;
        }

        this.clearReconnectTimer();
        this.setStatus(this.reconnectAttempts > 0 ? 'reconnecting' : 'connecting');

        try {
            const url = this.getWebSocketUrl();
            const WebSocketCtor = (typeof window !== 'undefined' && window.WebSocket) ? window.WebSocket : (typeof globalThis !== 'undefined' ? globalThis.WebSocket : null);
            
            if (!WebSocketCtor) {
                console.warn("[TableWebSocketClient] WebSocket API is not supported in this environment.");
                this.setStatus('offline');
                return;
            }

            this.socket = new WebSocketCtor(url);

            this.socket.onopen = () => {
                this.reconnectAttempts = 0;
                this.setStatus('connected');
            };

            this.socket.onmessage = (event) => {
                try {
                    const data = typeof event.data === 'string' ? JSON.parse(event.data) : event.data;
                    this.onEvent(data);
                } catch (err) {
                    console.warn("[TableWebSocketClient] Failed to parse message:", event.data, err);
                }
            };

            this.socket.onerror = (error) => {
                console.warn("[TableWebSocketClient] Connection error occurred:", error);
            };

            this.socket.onclose = (event) => {
                this.socket = null;
                // Code 1000 = Normal closure; 4401/4403 = Auth/permission rejection (no reconnect)
                if (this.isDestroyed || !this.shouldReconnect || event.code === 1000 || event.code === 4403 || event.code === 4401) {
                    this.setStatus('offline');
                    return;
                }
                this.scheduleReconnect();
            };
        } catch (err) {
            console.warn("[TableWebSocketClient] Could not establish WebSocket connection:", err);
            this.scheduleReconnect();
        }
    }

    scheduleReconnect() {
        if (this.isDestroyed || !this.shouldReconnect) {
            this.setStatus('offline');
            return;
        }

        this.setStatus('reconnecting');
        this.clearReconnectTimer();

        // Bounded exponential backoff with small jitter
        const delay = Math.min(
            this.baseReconnectDelay * Math.pow(this.backoffMultiplier, this.reconnectAttempts),
            this.maxReconnectDelay
        );
        const jitter = Math.random() * 500;
        const totalDelay = Math.round(delay + jitter);
        
        this.reconnectAttempts++;

        this.reconnectTimer = setTimeout(() => {
            if (!this.isDestroyed && this.shouldReconnect) {
                this.connect();
            }
        }, totalDelay);
    }

    clearReconnectTimer() {
        if (this.reconnectTimer) {
            clearTimeout(this.reconnectTimer);
            this.reconnectTimer = null;
        }
    }

    disconnect() {
        this.shouldReconnect = false;
        this.clearReconnectTimer();
        if (this.socket) {
            try {
                this.socket.close(1000, "Normal Closure");
            } catch (e) {}
            this.socket = null;
        }
        this.setStatus('offline');
    }

    destroy() {
        this.isDestroyed = true;
        this.disconnect();
    }
}

if (typeof window !== 'undefined') {
    window.TableWebSocketClient = TableWebSocketClient;
}

if (typeof module !== 'undefined' && module.exports) {
    module.exports = { TableWebSocketClient };
}
