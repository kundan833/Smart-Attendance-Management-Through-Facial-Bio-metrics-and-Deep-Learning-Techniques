document.addEventListener('DOMContentLoaded', function() {
    const startBtn = document.getElementById('startCapture');
    const submitBtn = document.getElementById('submitBtn');
    const captureModal = new bootstrap.Modal(document.getElementById('captureModal'));
    const progressBar = document.getElementById('captureProgress');
    const instructionText = document.getElementById('captureInstruction');
    const previewContainer = document.getElementById('capturePreview');
    const faceImagesInput = document.getElementById('faceImages');
    
    let capturedImages = [];
    let captureInterval;
    let captureCount = 0;
    const totalCaptures = 3;
    
    startBtn.addEventListener('click', async function() {
        capturedImages = [];
        previewContainer.innerHTML = '';
        captureCount = 0;
        captureModal.show();
        startCaptureSequence();
    });
    
    function startCaptureSequence() {
        const instructions = [
            "Please look straight at the camera",
            "Please turn your head slightly to the left",
            "Please turn your head slightly to the right"
        ];
        
        captureInterval = setInterval(async () => {
            if (captureCount >= totalCaptures) {
                clearInterval(captureInterval);
                completeCapture();
                return;
            }
            
            progressBar.style.width = `${((captureCount + 1) / totalCaptures) * 100}%`;
            instructionText.textContent = instructions[captureCount];
            
            try {
                const response = await fetch('/capture_frame');
                if (response.ok) {
                    const blob = await response.blob();
                    const reader = new FileReader();
                    
                    reader.onload = function() {
                        // Convert to base64 without data URL prefix
                        const base64data = reader.result.split(',')[1] || reader.result;
                        capturedImages.push(base64data);
                        
                        // Show preview
                        const previewCol = document.createElement('div');
                        previewCol.className = 'col-md-4';
                        previewCol.innerHTML = `
                            <div class="mb-2">
                                <img src="data:image/jpeg;base64,${base64data}" class="img-thumbnail" style="height: 100px;">
                                <div class="small text-center">${instructions[captureCount]}</div>
                            </div>
                        `;
                        previewContainer.appendChild(previewCol);
                        
                        captureCount++;
                    };
                    reader.readAsDataURL(blob);
                }
            } catch (error) {
                console.error('Capture error:', error);
            }
        }, 3000);
    }
    
    function completeCapture() {
        instructionText.textContent = "Capture complete!";
        progressBar.style.width = "100%";
        progressBar.classList.remove('progress-bar-animated');
        
        // Store images in hidden input
        faceImagesInput.value = JSON.stringify(capturedImages);
        submitBtn.disabled = false;
        
        setTimeout(() => {
            captureModal.hide();
        }, 2000);
    }
});
