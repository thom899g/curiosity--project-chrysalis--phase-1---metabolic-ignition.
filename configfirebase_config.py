"""
Firebase Configuration Module
Purpose: Centralized Firebase client initialization with robust error handling and credential validation.
Design: Uses singleton pattern to prevent multiple Firebase instances. Validates credentials before use.
Edge Cases: Handles missing credentials, invalid JSON, network connectivity issues, and permission errors.
"""

import os
import json
import logging
from pathlib import Path
from typing import Optional, Dict, Any
from dataclasses import dataclass
import firebase_admin
from firebase_admin import credentials, firestore, exceptions

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@dataclass
class FirebaseConfig:
    """Firebase configuration data class with validation"""
    project_id: str
    database_id: str = "(default)"
    collection_prefix: str = "chrysalis_"
    
class FirebaseClient:
    """Singleton Firebase client with automatic reconnection and health checks"""
    
    _instance = None
    _client = None
    _config = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(FirebaseClient, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
            
        self._config = self._load_config()
        self._initialize_firebase()
        self._initialized = True
    
    def _load_config(self) -> FirebaseConfig:
        """Load and validate Firebase configuration from environment"""
        try:
            # Check for credentials path
            creds_path = os.getenv("FIREBASE_CREDENTIALS_PATH")
            if not creds_path:
                raise ValueError("FIREBASE_CREDENTIALS_PATH environment variable not set")
            
            # Validate path exists
            if not Path(creds_path).exists():
                raise FileNotFoundError(f"Firebase credentials file not found: {creds_path}")
            
            # Validate JSON format
            with open(creds_path, 'r') as f:
                creds_data = json.load(f)
                project_id = creds_data.get("project_id")
                if not project_id:
                    raise ValueError("Invalid credentials: project_id missing")
            
            return FirebaseConfig(project_id=project_id)
            
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON in credentials file: {e}")
            raise
        except Exception as e:
            logger.error(f"Failed to load Firebase config: {e}")
            raise
    
    def _initialize_firebase(self) -> None:
        """Initialize Firebase Admin SDK with error recovery"""
        max_retries = 3
        for attempt in range(max_retries):
            try:
                # Check if already initialized
                if firebase_admin._apps:
                    logger.info("Firebase already initialized, reusing existing app")
                    app = firebase_admin.get_app()
                else:
                    # Initialize with credentials
                    creds_path = os.getenv("FIREBASE_CREDENTIALS_PATH")
                    cred = credentials.Certificate(creds_path)
                    app = firebase_admin.initialize_app(cred)
                    logger.info(f"Firebase initialized for project: {self._config.project_id}")
                
                # Test connection
                self._client = firestore.client(app)
                test_doc = self._client.collection("health_check").document("test")
                test_doc.set({"timestamp": firestore.SERVER_TIMESTAMP}, merge=True)
                test_doc.delete()
                
                logger.info("Firebase connection test successful")
                return
                
            except exceptions.FirebaseError as e:
                logger.error(f"Firebase initialization failed (attempt {attempt + 1}/{max_retries}): {e}")
                if attempt == max_retries - 1:
                    raise
                import time
                time.sleep(2 ** attempt)  # Exponential backoff
            except Exception as e:
                logger.error(f"Unexpected error during Firebase initialization: {e}")
                raise
    
    @property
    def client(self) -> firestore.Client:
        """Get Firestore client with health check"""
        if self._client is None:
            logger.warning("Firebase client not initialized, reinitializing...")
            self._initialize_firebase()
        return self._client
    
    @property
    def config(self) -> FirebaseConfig:
        """Get configuration"""
        return self._config
    
    def health_check(self) -> bool:
        """Perform health check on Firebase connection"""
        try:
            # Test write and read
            test_ref = self.client.collection("health_check").document("ping")
            test_data = {"timestamp": firestore.SERVER_TIMESTAMP}
            test_ref.set(test_data)
            
            # Read back
            doc = test_ref.get()
            if doc.exists:
                test_ref.delete()
                return True
            return False
        except Exception as e:
            logger.error(f"Firebase health check failed: {e}")
            return False

# Global instance
firebase_client = FirebaseClient()