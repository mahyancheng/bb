/* frontend/script.js */
document.addEventListener("DOMContentLoaded", () => {
  /* ─── DOM Handles ───────────────────────────────────────── */
  const $ = (id) => document.getElementById(id);
  const inp = $("userInput");
  const send = $("sendButton");
  const chat = $("chatHistory");
  const tasks = $("taskList");
  const plannerSel = $("plannerSel"); // Ensure this matches index.html
  const liveViewFrame = $("liveViewFrame");
  const liveViewStatus = $("liveViewStatus");

  console.log("DOM Loaded. Planner Select Element:", plannerSel);

  if (!plannerSel || !inp || !send || !chat || !tasks || !liveViewFrame || !liveViewStatus) {
      console.error("CRITICAL ERROR: One or more essential UI elements not found! Check IDs in index.html.");
      if(chat) appendToChat("FATAL ERROR: UI Initialization Failed. Check Console.", 'agent-error');
      return; // Stop script execution
  }

  let browserSel = null; // Initialize to null
  let codeSel = null;    // Initialize to null

  // VNC URL determination
  const vncHost = window.location.hostname;
  const vncPort = 6080; // Port exposed in docker-compose for noVNC
  const vncUrl = `http://${vncHost}:${vncPort}/vnc.html?host=${vncHost}&port=${vncPort}&autoconnect=true&resize=scale`;
  console.log("VNC URL determined as:", vncUrl);

  /* ─── Populate Model Dropdowns ──────────────────────────── */
  const makeSelect = (id, labelTxt, models) => {
    console.log(`Creating select for: ${labelTxt} with ID: ${id}`);
    const modelSelectionContainer = document.querySelector('.model-selection'); // Find the main container
    if (!modelSelectionContainer) {
         console.error("Cannot find '.model-selection' container div.");
         return null; // Cannot create select if container missing
    }

    const wrap  = document.createElement("div");
    wrap.style.marginTop = "8px";
    const label = document.createElement("label");
    label.htmlFor = id;
    label.textContent = labelTxt + ": ";
    label.style.marginRight = "5px";
    const sel = document.createElement("select");
    sel.id = id;

    try {
        if (!Array.isArray(models)) throw new Error("models parameter is not an array");
        models.forEach((m) => {
          const o = document.createElement("option");
          o.value = o.textContent = m;
          sel.appendChild(o);
        });
        label.appendChild(sel);
        wrap.appendChild(label);

        // Append the new wrapper div to the main model selection container
        modelSelectionContainer.appendChild(wrap);
        console.log(`Appended select ${id} to .model-selection container.`);

    } catch (e) {
        console.error(`Error creating/appending options for ${id}:`, e);
        return null; // Return null on error
    }
    return sel; // Return the created select element
  };

  // Fetch models from the backend API
  console.log("Fetching models from /api/models...");
  fetch("/api/models")
    .then((r) => {
        console.log("API Response Status:", r.status, r.statusText);
        if (!r.ok) {
            return r.text().then(text => { // Try to get error text from response
                 throw new Error(`API request failed: ${r.status} ${r.statusText}. Response: ${text}`);
            });
        }
        return r.json();
    })
    .then((data) => {
      console.log("API Response Data Received:", data);
      if (!data || !data.models) {
          throw new Error("Invalid or missing 'models' key in API response.");
      }
      const models = data.models;
      console.log("Extracted models:", models);

      if (!Array.isArray(models)) {
          console.error("API response 'models' is not an array:", models);
          throw new Error("Invalid model data format from API.");
      }
      if (models.length === 0) { // Handle empty model list
          console.warn("No models found via API. Check backend/Ollama.");
           plannerSel.innerHTML = ''; // Clear loading message
           const o = document.createElement("option");
           o.value = ""; o.textContent = "No Models Found!"; o.disabled = true;
           plannerSel.appendChild(o);
           appendToChat("Warning: No LLM models found via backend API. Check Ollama connection.\n", 'agent-warning');
           return; // Stop if no models
      }

      // --- Populate Planner Select ---
      try {
          console.log("Populating planner select...");
          plannerSel.innerHTML = ''; // Clear "Loading..." option
          models.forEach((m) => {
            const o = document.createElement("option");
            o.value = o.textContent = m;
            plannerSel.appendChild(o);
          });
          // Set default selection (optional, based on backend defaults if needed)
          if (models.includes("llama3:latest")) plannerSel.value = "llama3:latest";
          console.log("Planner select populated.");
      } catch (e) {
           console.error("Error populating planner select:", e);
           appendToChat("Error: Failed to populate planner dropdown.\n", 'agent-error');
      }

      // --- Create and populate the other selects ---
      browserSel = makeSelect("browserSel", "Browser LLM", models);
      codeSel    = makeSelect("codeSel",    "Code LLM",    models);

      // Set defaults for other selects (optional)
      if (browserSel && models.includes("qwen2.5:7b")) browserSel.value = "qwen2.5:7b";
      if (codeSel && models.includes("deepcoder:latest")) codeSel.value = "deepcoder:latest";

    })
    .catch((e) => {
      console.error("Model load fetch or processing failed:", e);
      appendToChat(`Error: Failed to load models from backend (${e.message}). Please check backend logs & Ollama. Using default.\n`, 'agent-error');
      plannerSel.innerHTML = ''; // Clear loading message
      ["llama3:latest", "(Error Loading)"].forEach((m) => {
        const o = document.createElement("option");
        o.value = (m === "(Error Loading)") ? "" : m;
        o.textContent = m;
        if (m === "(Error Loading)") o.disabled = true;
        plannerSel.appendChild(o);
      });
      // Ensure other selects are not created if fetch failed
      browserSel = null;
      codeSel = null;
    });

  /* ─── WebSocket Glue ────────────────────────────────────── */
  const wsProto = location.protocol === "https:" ? "wss:" : "ws:";
  const wsURL   = `${wsProto}//${location.hostname}:${location.port || (wsProto === "wss:" ? 443: 80)}/ws`; // Use current port
  console.log("WebSocket URL:", wsURL);
  let ws;
  let connectAttempts = 0;
  const MAX_CONNECT_ATTEMPTS = 5;
  let reconnectTimeout = null;

  // --- Utility to append messages to chat ---
  const appendToChat = (text, type = 'agent-log', isUser = false) => {
       if (!chat) return;
       // Clear initial placeholder if it exists
       const placeholder = chat.querySelector('p.system-info');
       if (placeholder && placeholder.textContent.includes("Connecting")) {
            placeholder.remove();
       }

       const messageElement = document.createElement('p');
       messageElement.classList.add('message', type);
       if (isUser) {
            messageElement.classList.add('user'); // Add user class for specific styling
       }

       // Basic Markdown code block formatting
       const escapedText = text.replace(/</g, "&lt;").replace(/>/g, "&gt;");
       const formattedText = escapedText.replace(/```([\s\S]*?)```/g, '<pre><code>$1</code></pre>');
       messageElement.innerHTML = formattedText.replace(/\n/g, '<br>'); // Convert newlines

       chat.appendChild(messageElement);
       // Scroll to bottom (use requestAnimationFrame for smoother scroll after render)
       requestAnimationFrame(() => {
            chat.scrollTop = chat.scrollHeight;
       });
   };

   // --- Utility to update task list ---
   const updateTaskList = (tasksData) => {
       if (!tasks) return;
       if (!Array.isArray(tasksData) || tasksData.length === 0) {
           tasks.innerHTML = '<p class="system-info">No tasks planned or tasks cleared.</p>';
           return;
       }

       tasks.innerHTML = ''; // Clear previous tasks
       tasksData.forEach((task, index) => {
           const taskElement = document.createElement('div');
           const status = task.status || 'pending';
           taskElement.classList.add('task-item', `status-${status}`);

           const statusIcon = document.createElement('span');
           statusIcon.classList.add('task-status'); // Icon handled by CSS

           const description = document.createElement('span');
           description.classList.add('task-description');
           description.textContent = task.description || `Task ${index + 1}`; // Use description or default

           taskElement.appendChild(statusIcon);
           taskElement.appendChild(description);
           tasks.appendChild(taskElement);
       });
       tasks.scrollTop = tasks.scrollHeight; // Scroll task list
   };


  // --- WebSocket Connection Logic ---
  const connect = () => {
      if (reconnectTimeout) clearTimeout(reconnectTimeout); // Clear any pending retry timer
      if (connectAttempts >= MAX_CONNECT_ATTEMPTS) {
           appendToChat("Error: Max WebSocket connection attempts reached. Please refresh or check backend.\n", 'agent-error');
           updateVncStatus("Connection Failed", true);
           return;
      }
      console.log(`Attempting WebSocket connection (Attempt ${connectAttempts + 1})...`);
      ws = new WebSocket(wsURL);
      connectAttempts++;
      updateVncStatus("Connecting...", true);


      ws.onopen    = () => {
          appendToChat("✓ WebSocket connected\n", 'system-info');
          connectAttempts = 0; // Reset on success
          // Load VNC Frame now that WS is connected (signals backend is likely ready)
          loadVncFrame();
      };

      ws.onmessage = ({data}) => {
          // Route messages based on prefix
          if (data.startsWith("TASK_LIST_UPDATE:")) {
               try {
                   const taskDataJson = data.substring("TASK_LIST_UPDATE:".length);
                   const tasksArray = JSON.parse(taskDataJson);
                   updateTaskList(tasksArray);
               } catch (e) {
                   console.error("Failed to parse task list update:", e, "Data:", data);
                   appendToChat(`Agent Warning: Received malformed task list data: ${data.substring(0,100)}...\n`, 'agent-warning');
               }
          } else {
               // Determine message type for styling (can refine prefixes)
               let messageType = 'agent-log'; // Default
               if (data.startsWith("Agent Error:")) messageType = 'agent-error';
               else if (data.startsWith("Agent Warning:")) messageType = 'agent-warning';
               else if (data.startsWith("Tool Input:")) messageType = 'tool-input';
               else if (data.startsWith("Tool Output:")) messageType = 'tool-output';
               else if (data.startsWith("Agent: Final Answer:")) messageType = 'agent-final';
               else if (data.startsWith("**Agent:")) messageType = 'agent-important'; // For bolded messages
               else if (data.startsWith("Agent:")) messageType = 'agent-log';

               appendToChat(data + "\n", messageType); // Add newline for readability in basic append
          }
      };

      ws.onclose   = (event) => {
           const reason = event.reason ? ` Reason: ${event.reason}` : '';
           console.log(`WebSocket closed. Code: ${event.code}, Clean: ${event.wasClean}, Reason: ${reason}`);
           appendToChat(`⚠ WebSocket closed (Code: ${event.code}${reason}).\n`, 'agent-warning');
           ws = null; // Ensure ws variable is cleared
           unloadVncFrame("Connection Closed");
           // Retry connection only if not a clean close and attempts remain
           if (!event.wasClean && event.code !== 1000 && event.code !== 1001 && connectAttempts < MAX_CONNECT_ATTEMPTS) {
                appendToChat(`Retrying connection in 3s...\n`, 'agent-warning');
                if (reconnectTimeout) clearTimeout(reconnectTimeout); // Clear existing timer if any
                reconnectTimeout = setTimeout(connect, 3000);
           } else if (connectAttempts >= MAX_CONNECT_ATTEMPTS) {
                appendToChat("Max connection retries reached.\n", 'agent-error');
           }
      };

      ws.onerror = (error) => {
           console.error("WebSocket Error:", error);
           appendToChat("Error: WebSocket connection error. Check console.\n", 'agent-error');
           updateVncStatus("Connection Error", true);
           // onclose will usually be called after onerror, triggering the retry logic there
      };
  };

  // --- VNC Frame Handling ---
  const updateVncStatus = (text, show = false) => {
      if (!liveViewStatus) return;
      liveViewStatus.textContent = text;
      if (show) {
           liveViewStatus.classList.add('visible');
      } else {
           liveViewStatus.classList.remove('visible');
      }
  };

  const loadVncFrame = () => {
      if (!liveViewFrame) return;
      console.log("Loading VNC iframe src:", vncUrl);
      liveViewFrame.src = vncUrl;
      // Hide status message after a delay, assuming connection works
      updateVncStatus("Loading VNC...", true);
      setTimeout(() => { updateVncStatus("", false); }, 3000); // Hide after 3s
  };

  const unloadVncFrame = (reason = "Disconnected") => {
       if (!liveViewFrame) return;
       liveViewFrame.src = 'about:blank'; // Clear the iframe
       updateVncStatus(reason, true); // Show status message
  };

  // Initial connection attempt
  appendToChat("Connecting to agent...\n", 'system-info'); // Show initial connecting message
  connect();

  /* ─── Send User Query ────────────────────────────────────── */
  const push = () => {
    if (!ws || ws.readyState !== WebSocket.OPEN) {
        appendToChat("Error: WebSocket not connected. Cannot send message.\n", 'agent-error');
        return;
    }
    const queryText = inp.value.trim();
    if (!queryText) return; // Don't send empty messages

    // Get selected models, ensuring selects exist
    const plannerValue = plannerSel ? plannerSel.value : 'llama3:latest'; // Fallback
    const browserValue = browserSel ? browserSel.value : plannerValue; // Fallback to planner
    const codeValue    = codeSel    ? codeSel.value    : plannerValue; // Fallback to planner

    // Validate that a model is selected (value is not empty)
    if (!plannerValue || (browserSel && !browserValue) || (codeSel && !codeValue)) {
         appendToChat("Error: Please select a valid model for all dropdowns.\n", 'agent-error');
         return;
    }

    const payload = {
      query:         queryText,
      planner_model: plannerValue,
      browser_model: browserValue,
      code_model:    codeValue,
    };

    appendToChat(queryText, 'user', true); // Display user message, styled as user

    ws.send(JSON.stringify(payload));
    inp.value = ""; // Clear input after sending
    inp.rows = 3; // Reset textarea size
    tasks.innerHTML = '<p class="system-info">Agent processing request...</p>'; // Clear task list
  };

  // --- Event Listeners ---
  send.onclick = push;
  inp.addEventListener("keypress", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); push(); }
  });
  // Auto-resize textarea
  inp.addEventListener('input', () => {
      const minRows = 3;
      const maxRows = 10;
      inp.rows = minRows; // Reset rows
      const currentRows = Math.ceil(inp.scrollHeight / parseFloat(getComputedStyle(inp).lineHeight));
      inp.rows = Math.max(minRows, Math.min(currentRows, maxRows));
  });

}); // End DOMContentLoaded