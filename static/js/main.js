document.addEventListener('DOMContentLoaded', function() {
    // Elements
    const captureButtons = {
        front: document.getElementById('captureFront'),
        left: document.getElementById('captureLeft'),
        right: document.getElementById('captureRight')
    };
    
    const previewImages = {
        front: document.getElementById('previewFront'),
        left: document.getElementById('previewLeft'),
        right: document.getElementById('previewRight')
    };
    
    const statusElements = {
        front: document.getElementById('statusFront'),
        left: document.getElementById('statusLeft'),
        right: document.getElementById('statusRight')
    };
    
    const registerBtn = document.getElementById('registerBtn');
    const faceImagesInput = document.getElementById('face_images');
    
    // State
    const faceImages = {
        front: null,
        left: null,
        right: null
    };
    
    // Capture image handlers
    Object.keys(captureButtons).forEach(angle => {
        captureButtons[angle].addEventListener('click', async () => {
            try {
                // Show loading state
                statusElements[angle].textContent = 'Capturing...';
                statusElements[angle].style.color = '#3498db';
                
                const response = await fetch('/capture_frame');
                if (response.ok) {
                    const blob = await response.blob();
                    const reader = new FileReader();
                    
                    reader.onload = () => {
                        // Update preview
                        previewImages[angle].src = reader.result;
                        faceImages[angle] = reader.result;
                        
                        // Update status
                        statusElements[angle].textContent = 'Captured ✓';
                        statusElements[angle].style.color = '#2ecc71';
                        
                        // Update form state
                        updateFormState();
                    };
                    
                    reader.readAsDataURL(blob);
                } else {
                    throw new Error('Failed to capture image');
                }
            } catch (error) {
                console.error('Error:', error);
                statusElements[angle].textContent = 'Error!';
                statusElements[angle].style.color = '#e74c3c';
                setTimeout(() => {
                    statusElements[angle].textContent = '';
                }, 2000);
            }
        });
    });
    
    // Update form submission state
    function updateFormState() {
        const allCaptured = Object.values(faceImages).every(img => img !== null);
        
        if (allCaptured) {
            registerBtn.disabled = false;
            faceImagesInput.value = JSON.stringify([
                faceImages.front,
                faceImages.left,
                faceImages.right
            ]);
        } else {
            registerBtn.disabled = true;
        }
    }
    
    // Form validation
    document.getElementById('registrationForm').addEventListener('submit', function(e) {
        const studentId = document.getElementById('student_id').value.trim();
        const name = document.getElementById('name').value.trim();
        const stream = document.getElementById('stream').value;
        
        if (!studentId || !name || !stream) {
            e.preventDefault();
            alert('Please fill all required fields');
            return false;
        }
        
        if (!faceImages.front || !faceImages.left || !faceImages.right) {
            e.preventDefault();
            alert('Please capture all three face angles');
            return false;
        }
        
        // Show loading state
        registerBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Processing...';
        registerBtn.disabled = true;
    });
    
    // Add input validation
    document.getElementById('student_id').addEventListener('input', function() {
        this.value = this.value.toUpperCase();
    });
});
