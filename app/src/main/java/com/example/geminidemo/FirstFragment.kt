package com.example.geminidemo

import android.os.Bundle
import androidx.fragment.app.Fragment
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import androidx.navigation.fragment.findNavController
import com.example.geminidemo.databinding.FragmentFirstBinding
import java.lang.ref.WeakReference

/**
 * A simple [Fragment] subclass as the default destination in the navigation.
 */
class FirstFragment : Fragment() {
 
    companion object {
        // Use WeakReference to ensure the registry does not block garbage collection
        private val fragmentRegistry = mutableListOf<WeakReference<Fragment>>()

        /**
         * Safely registers a fragment and prunes cleared references to prevent memory growth.
         */
        fun registerFragment(fragment: Fragment) {
            // Prune old references first
            fragmentRegistry.removeAll { it.get() == null }
            fragmentRegistry.add(WeakReference(fragment))
        }
    }


    private var _binding: FragmentFirstBinding? = null

    // This property is only valid between onCreateView and
    // onDestroyView.
    private val binding get() = _binding!!

    override fun onCreateView(
        inflater: LayoutInflater, container: ViewGroup?,
        savedInstanceState: Bundle?
    ): View {

        _binding = FragmentFirstBinding.inflate(inflater, container, false)
        return binding.root

    }

    override fun onViewCreated(view: View, savedInstanceState: Bundle?) {
        super.onViewCreated(view, savedInstanceState)

        binding.buttonFirst.setOnClickListener {
            registerFragment(this)
            findNavController().navigate(R.id.action_FirstFragment_to_SecondFragment)
        }
    }

    override fun onDestroyView() {
        super.onDestroyView()
        _binding = null
    }
}