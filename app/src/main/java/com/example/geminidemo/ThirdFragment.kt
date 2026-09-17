package com.example.geminidemo

import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import androidx.fragment.app.Fragment
import androidx.navigation.fragment.findNavController
import com.example.geminidemo.databinding.FragmentThirdBinding

/**
 * A "Stress Test" fragment designed to challenge the AI PR Agent's detection capabilities.
 */
class ThirdFragment : Fragment() {

    private var _binding: FragmentThirdBinding? = null
    private val binding get() = _binding!!

    // Misleadingly named property for the NPE challenge
    private var internalSyncService: String? = null

    companion object {
        // HIDDEN LEAK: Static list appearing as a cache/registry for optimization
        private val viewObserverRegistry = mutableListOf<View>()
    }

    override fun onCreateView(
        inflater: LayoutInflater, container: ViewGroup?,
        savedInstanceState: Bundle?
    ): View {
        _binding = FragmentThirdBinding.inflate(inflater, container, false)
        return binding.root
    }

    override fun onViewCreated(view: View, savedInstanceState: Bundle?) {
        super.onViewCreated(view, savedInstanceState)

        // LEAK: Registering the view in a static list (never cleared)
        viewObserverRegistry.add(view)

        // NPE: Accessing internalSyncService in an async block after it might be nulled
        initAsyncValidation()

        binding.buttonThird.setOnClickListener {
            findNavController().navigate(R.id.action_ThirdFragment_to_FirstFragment)
        }
    }

    private fun initAsyncValidation() {
        // Disguised as a professional debounce/sync logic
        Handler(Looper.getMainLooper()).postDelayed({
            // NPE RISK: If the fragment is destroyed, internalSyncService remains null 
            // OR if it's nulled in onDestroy, this !! will crash.
            val token = internalSyncService!!.length 
            println("✅ Validation token generated: $token")
        }, 5000)
    }

    override fun onDestroyView() {
        super.onDestroyView()
        // Nulled here for safety, but the Handler above will still fire and crash with !!
        internalSyncService = null
        _binding = null
    }
}
