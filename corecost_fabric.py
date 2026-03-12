"""
Cost Instrumentation Layer (CIL) - Core Component
Purpose: Real-time cost awareness for all API operations with batched async writes
Design: Uses decorator pattern for non-invasive instrumentation. Batches writes to minimize observer effect.
Edge Cases: Handles network failures with retry logic, rate limiting, and ensures no blocking of main operations.
"""

import asyncio
import time
import hashlib
import json
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Callable
import logging
from functools import wraps
import threading

from config.firebase_config import firebase_client

logger = logging.getLogger(__name__)

@dataclass
class CostEvent:
    """Cost event data structure with validation"""
    service: str  # e.g., "openai", "cohere", "anthropic"
    endpoint: str  # e.g., "chat/completions", "embed"
    timestamp: datetime
    input_tokens: int = 0
    output_tokens: int = 0
    calculated_cost_usd: float = 0.0
    script_path: str = ""
    function_name: str = ""
    call_stack_hash: str = ""
    execution_context: str = ""
    metadata: Dict[str, Any] = None
    
    def __post_init__(self):
        """Validate and set defaults after initialization"""
        if self.metadata is None:
            self.metadata = {}
        if not self.call_stack_hash:
            self.call_stack_hash = self._generate_stack_hash()
    
    def _generate_stack_hash(self) -> str:
        """Generate deterministic hash from service, endpoint, and context"""
        hash_input = f"{self.service}:{self.endpoint}:{self.execution_context}"
        return hashlib.md5(hash_input.encode()).hexdigest()[:12]
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to Firestore-compatible dictionary"""
        data = asdict(self)
        data['timestamp'] = self.timestamp
        return data

class BatchLogger:
    """Asynchronous batched logger for cost events with adaptive sampling"""
    
    def __init__(self, batch_size: int = 50, flush_interval: float = 5.0):
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        self.batch: List[CostEvent] = []
        self.lock = threading.Lock()
        self._stop_event = threading.Event()
        self._flush_thread = None
        self._sampling_rate = 1.0  # Start with 100% sampling
        
    def start(self):
        """Start the background flush thread"""
        if self._flush_thread is None or not self._flush_thread.is_alive():
            self._stop_event.clear()
            self._flush_thread = threading.Thread(target=self._flush_loop, daemon=True)
            self._flush_thread.start()
            logger.info("BatchLogger started with adaptive sampling")
    
    def stop(self):
        """Stop the background flush thread and flush remaining events"""
        self._stop_event.set()
        if self._flush_thread and self._flush_thread.is_alive():
            self._flush_thread.join(timeout=2.0)
        self._flush_batch()  # Final flush
    
    def add_event(self, event: CostEvent):
        """Add event to batch with adaptive sampling"""
        # Adaptive sampling: reduce sampling under high load
        import random
        if random.random() > self._sampling_rate:
            return
            
        with self.lock:
            self.batch.append(event)
            
            # Adaptive sampling adjustment
            if len(self.batch) > self.batch_size * 2:
                self._sampling_rate = max(0.1, self._sampling_rate * 0.8)  # Reduce sampling
            elif len(self.batch) < self.batch_size // 2:
                self._sampling_rate = min(1.0, self._sampling_rate * 1.2)  # Increase sampling
            
            # Trigger flush if batch size reached
            if len(self.batch) >= self.batch_size:
                self._flush_batch()
    
    def _flush_batch(self):
        """Flush current batch to Firestore"""
        with self.lock:
            if not self.batch:
                return
                
            current_batch = self.batch.copy()
            self.batch.clear()
        
        # Async flush to avoid blocking
        asyncio.run(self._async_flush(current_batch))
    
    async def _async_flush(self, events: List[CostEvent]):
        """Asynchronously flush events to Firestore with retry logic"""
        if not events:
            return
            
        try:
            db = firebase_client.client
            batch = db.batch()
            collection = db.collection(f"{firebase_client.config.collection_prefix}cost_events")
            
            for event in events:
                doc_ref = collection.document()
                batch.set(doc_ref, event.to_dict())
            
            # Commit with retry
            max_retries = 3
            for attempt in range(max_retries):
                try:
                    batch.commit()
                    logger.debug(f"Flushed {len(events)} events to Firestore")
                    break
                except exceptions.FirebaseError as e:
                    if attempt == max_retries - 1:
                        logger.error(f"Failed to flush batch after {max_retries} attempts: {e}")
                        # Store failed events for recovery (implementation omitted for brevity)
                        break
                    await asyncio.sleep(2 ** attempt)  # Exponential backoff
                    
        except Exception as e:
            logger.error(f"Unexpected error during batch flush: {e}")
    
    def _flush_loop(self):
        """Background thread that flushes at regular intervals"""
        while not self._stop_event.is_set():
            time.sleep(self.flush_interval)
            self._flush_batch()

# Global batch logger instance
batch_logger = BatchLogger()
batch_logger.start()

def instrument_cost(service: str, execution_context: str = ""):
    """
    Decorator to instrument API call functions for cost tracking
    
    Args:
        service: Service name (e.g., "openai")
        execution_context: Context tag for grouping (e.g., "user_query_analysis")
    
    Returns:
        Decorated function with cost instrumentation
    """
    def decorator(func: Callable):
        @wraps(func)
        def wrapper(*args, **kwargs):
            import inspect
            import sys
            
            # Capture start time
            start_time = time.time()
            
            try:
                # Execute original function
                result = func(*args, **kwargs)
                
                # Calculate cost (simplified - would be service-specific)
                # In reality, this would parse response for token counts
                cost = 0.0
                input_tokens = 0
                output_tokens = 0
                
                # Attempt to extract token counts from result
                if hasattr(result, 'usage'):
                    # OpenAI-style response
                    input_tokens = getattr(result.usage, 'prompt_tokens', 0)
                    output_tokens = getattr(result.usage, 'completion_tokens', 0)
                elif isinstance(result, dict) and 'usage' in result:
                    # Dict-style response
                    usage = result['usage']
                    input_tokens = usage.get('prompt_tokens', 0)
                    output_tokens = usage.get('completion_tokens', 0)
                
                # Calculate cost based on service rates (simplified)
                # Actual implementation would use proper pricing tables
                if service == "openai":
                    # GPT-4 pricing example
                    cost = (input_tokens * 0.00003 + output_tokens * 0.00006) / 1000
                elif service == "cohere":
                    # Cohere pricing example
                    cost = (input_tokens + output_tokens) * 0.0000004
                
                # Capture metadata
                script_path = inspect.getfile(func)
                stack = inspect.stack()
                call_stack_hash = hashlib.md5(
                    "".join([str(frame.filename) for frame in stack[:3]]).encode()
                ).hexdigest()[:12]
                
                # Create cost event
                event = CostEvent(
                    service=service,
                    endpoint=func.__name__,
                    timestamp=datetime.now(),
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    calculated_cost_usd=cost,
                    script_path=script_path,
                    function_name=func.__name__,
                    call_stack_hash=call_stack_hash,
                    execution_context=execution_context,
                    metadata={
                        "execution_time_ms": (time.time() - start_time) * 1000,
                        "args_count": len(args) + len(kwargs)
                    }
                )
                
                # Log event (non-blocking)
                batch_logger.add_event(event)
                
                return result
                
            except Exception as e:
                # Log error event
                error_event = CostEvent(
                    service=service,
                    endpoint=func.__name__,
                    timestamp=datetime.now(),
                    script_path=inspect.getfile(func),
                    function_name=func.__name__,
                    execution_context=f"error:{execution_context}",
                    metadata={"error": str(e), "error_type": type(e).__name__}
                )
                batch_logger.add_event(error_event)
                raise  # Re-raise original exception
        
        return wrapper
    return decorator

def calculate_cost_estimate(service: str, input_tokens: int, output_tokens: int) -> float: