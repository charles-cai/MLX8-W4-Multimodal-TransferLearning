import os
import sqlite3
from datetime import datetime
import litellm
from pathlib import Path
import base64
import contextlib

class FlaticonDatasets:
    # Constants
    DEFAULT_BASE_FOLDER = "../.data/flaticon.com/target/train"
    DEFAULT_DB_FILE = "../.data/flaticon_vision_text.sqlite3"
    DEFAULT_MODEL = "ollama/qwen2.5vl"
    IMAGE_EXTENSIONS = ('.png', '.jpg', '.jpeg', '.gif', '.bmp', '.svg')
    DEFAULT_BATCH_SIZE = 10
    PROCESSING_COUNT_ID = 0
    
    def __init__(self, base_folder=None, db_file=None):
        self.base_folder = base_folder or self.DEFAULT_BASE_FOLDER
        self.db_file = db_file or self.DEFAULT_DB_FILE
        self.model = self.DEFAULT_MODEL
        self._init_database()
    
    @contextlib.contextmanager
    def _get_db_connection(self):
        """Context manager for database connections with proper cleanup"""
        conn = sqlite3.connect(self.db_file)
        # Enable WAL mode for better crash resistance
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA synchronous=NORMAL')
        try:
            yield conn
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    
    def _init_database(self):
        """Initialize SQLite database with required schema"""
        with self._get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS flaticon_images (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    collection TEXT NOT NULL,
                    type TEXT NOT NULL,
                    file TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    image_path TEXT UNIQUE NOT NULL,
                    model TEXT NULL,
                    text TEXT NULL,
                    skipped INTEGER DEFAULT 0,
                    created_when TIMESTAMP NOT NULL,
                    scanned_when TIMESTAMP NOT NULL,
                    updated_when TIMESTAMP NULL
                )
            ''')
            # Add skipped column to existing table if it doesn't exist
            try:
                cursor.execute('ALTER TABLE flaticon_images ADD COLUMN skipped INTEGER DEFAULT 0')
            except sqlite3.OperationalError:
                # Column already exists
                pass
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS processing_count (
                    id INTEGER PRIMARY KEY CHECK (id = 0),
                    image_path TEXT,
                    updated_when TIMESTAMP
                )
            ''')
            # Initialize count record if it doesn't exist
            cursor.execute('INSERT OR IGNORE INTO processing_count (id, image_path, updated_when) VALUES (0, NULL, NULL)')
            conn.commit()
    
    def _load_last_processed(self):
        """Load the last processed image path from database"""
        with self._get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT image_path FROM processing_count WHERE id = 0')
            result = cursor.fetchone()
            return result[0] if result and result[0] else None
    
    def _save_last_processed(self, image_path):
        """Save the last processed image path to database"""
        with self._get_db_connection() as conn:
            cursor = conn.cursor()
            current_time = datetime.now()
            cursor.execute(
                'UPDATE processing_count SET image_path = ?, updated_when = ? WHERE id = 0',
                (image_path, current_time)
            )
            conn.commit()
    
    def _get_image_files(self):
        """Get all image files from the base folder"""
        image_files = []
        for root, dirs, files in os.walk(self.base_folder):
            for file in files:
                if file.lower().endswith(self.IMAGE_EXTENSIONS):
                    image_files.append(os.path.join(root, file))
        return sorted(image_files)  # Sort for consistent ordering
    
    def _parse_image_path(self, image_path):
        """Parse image path to extract metadata"""
        # e.g., '../.data/flaticon.com/_corrupt/train/134332-business-set/png/atm-1.png'
        parts = image_path.split('/')
        collection_folder = parts[-3]  # '134332-business-set'
        type_folder = parts[-2]        # 'png'
        filename = parts[-1]           # 'atm-1.png'
        
        # Extract filename without extension
        file_base = filename.rsplit('.', 1)[0]  # 'atm-1'
        
        return {
            'collection': collection_folder,
            'type': type_folder,
            'file': file_base,
            'filename': filename
        }
    
    def _image_to_base64(self, image_path):
        with open(image_path, "rb") as image_file:
            base64_image = base64.b64encode(image_file.read()).decode('utf-8')
        return base64_image 

    def _create_prompt(self, image_path):
        """Create prompt text for vision model"""
        metadata = self._parse_image_path(image_path)
        collection_parts = metadata['collection'].split('-', 1)
        collection_num = collection_parts[0]
        collection_name = collection_parts[1] if len(collection_parts) > 1 else ""
        
        return f"""
This is from Flaticon.com Collection #{collection_num} \"{collection_name}\", {metadata['file']} {metadata['type']}.
Describe what's in this image in a very very concise way, pay attention to the designer's original intention for a {metadata['file']} logo,
under the collection of \"{collection_name}\"), only mention if details like color scheme, lines, layout, style, etc warrant mentioning. 
Don't use full sentence, more like a description from an art gallery description for a painting.
"""
    
    def _process_image(self, image_path):
        """Process a single image with the vision model"""
        try:
            base64_image = self._image_to_base64(image_path)
            prompt_text = self._create_prompt(image_path)
            
            response = litellm.completion(
                model=self.model,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": prompt_text
                            },
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": base64_image
                                }
                            }
                        ]
                    }
                ],
            )
            
            return response.choices[0].message.content
        except Exception as e:
            print(f"Error processing {image_path}: {e}")
            return None
    
    def _scan_and_update_database(self):
        """Scan image files and update database with timestamps"""
        image_files = self._get_image_files()
        current_time = datetime.now()
        
        print(f"Scanning {len(image_files)} image files...")
        
        with self._get_db_connection() as conn:
            cursor = conn.cursor()
            
            # Process in batches to avoid long transactions
            batch_size = 100
            for i in range(0, len(image_files), batch_size):
                batch = image_files[i:i+batch_size]
                
                for image_path in batch:
                    metadata = self._parse_image_path(image_path)
                    
                    # Check if file exists in database
                    cursor.execute('SELECT id FROM flaticon_images WHERE image_path = ?', (image_path,))
                    existing = cursor.fetchone()
                    
                    if existing:
                        # Update scanned_when for existing file
                        cursor.execute(
                            'UPDATE flaticon_images SET scanned_when = ? WHERE image_path = ?',
                            (current_time, image_path)
                        )
                    else:
                        # Insert new file with created_when and scanned_when
                        cursor.execute('''
                            INSERT INTO flaticon_images 
                            (collection, type, file, filename, image_path, created_when, scanned_when)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                        ''', (
                            metadata['collection'],
                            metadata['type'], 
                            metadata['file'],
                            metadata['filename'],
                            image_path,
                            current_time,
                            current_time
                        ))
                
                # Commit each batch
                conn.commit()
                print(f"Processed batch {i//batch_size + 1}/{(len(image_files) + batch_size - 1)//batch_size}")
        
        print("Database scanning complete!")
    
    def _process_unprocessed_records(self, batch_size=None):
        """Process records that don't have updated_when timestamp"""
        if batch_size is None:
            batch_size = self.DEFAULT_BATCH_SIZE
            
        with self._get_db_connection() as conn:
            cursor = conn.cursor()
            
            # Get unprocessed records that are not skipped
            cursor.execute('''
                SELECT id, image_path FROM flaticon_images 
                WHERE updated_when IS NULL AND skipped = 0
                ORDER BY id
            ''')
            unprocessed = cursor.fetchall()
            
            print(f"Found {len(unprocessed)} unprocessed records (excluding skipped)")
            
            processed_count = 0
            for record_id, image_path in unprocessed:
                print(f"Processing record {record_id}: {image_path}")
                
                try:
                    # Process with vision model
                    vision_text = self._process_image(image_path)
                    
                    if vision_text:
                        # Update record with vision text in a separate transaction
                        current_time = datetime.now()
                        
                        cursor.execute('''
                            UPDATE flaticon_images 
                            SET model = ?, text = ?, updated_when = ?
                            WHERE id = ?
                        ''', (self.model, vision_text, current_time, record_id))
                        
                        conn.commit()
                        processed_count += 1
                        
                        # Update last processed record
                        self._save_last_processed(image_path)
                        
                        if processed_count % batch_size == 0:
                            print(f"Processed {processed_count} records so far...")
                    else:
                        print(f"Skipped record {record_id} due to processing error")
                        
                except Exception as e:
                    print(f"Error processing record {record_id}: {e}")
                    # Continue with next record instead of stopping
                    continue
            
            print(f"Processing complete! Processed {processed_count} records.")
    
    def Generate(self, batch_size=None):
        """Main method to scan files and process unprocessed records"""
        if batch_size is None:
            batch_size = self.DEFAULT_BATCH_SIZE
            
        print("Starting FlaticonDatasets processing...")
        
        # Step 1: Scan files and update database
        self._scan_and_update_database()
        
        # Step 2: Process unprocessed records
        self._process_unprocessed_records(batch_size)
        
        print("All processing complete!")

if __name__ == "__main__":
    dataset = FlaticonDatasets()
    dataset.Generate()